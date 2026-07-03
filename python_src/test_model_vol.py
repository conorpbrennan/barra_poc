"""
test_model_vol.py — accuracy checks for the `Model vol` cube measure (the reference risk
number, sigma = sqrt(x'Fx + w'dw), added 2026-07-03).

All INTEG (need the live backend on :8010; SKIP if down) — the measure lives in the cube, so
there is no pure-unit surface; accuracy is established by tying the cube number out against the
two INDEPENDENT numpy implementations of the same sigma:

  * /contributions vol_1d      — _euler_contributions: sqrt(x'Fx + w'dw), F = np.cov(history)
  * /whatif before.model_vol_1d — _risk_from_weights: same sigma, separate code path
  * the in-cube identity Model vol^2 = Scenario PnL vol^2 + Specific variance
  * slicing behaviour: sector cells positive, sub-additive vs the book (diversification)
  * scenario-set semantics: Evt window vol differs from HistFull; length-1 Hypo is degenerate

Run:  BARRA_API=http://127.0.0.1:8010 ../barra/bin/python test_model_vol.py
"""
from __future__ import annotations
import json
import os
import urllib.parse

API = os.environ.get("BARRA_API", "http://127.0.0.1:8010")
INTEG = []
DATE = None


def integ(fn):
    INTEG.append(fn); return fn


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


def _pivot(rows="", measures="", filters=None):
    import requests
    q = {"rows": rows, "measures": measures, "filters": json.dumps(filters or {})}
    r = requests.get(f"{API}/pivot?{urllib.parse.urlencode(q)}", timeout=120)
    r.raise_for_status()
    return r.json()


def _book_cell(measures, scen="HistFull"):
    j = _pivot(rows="ScenarioSet", measures=measures,
               filters={"Book": ["Soros"], "Date": [DATE], "ScenarioSet": [scen]})
    assert j["records"], j
    return j["records"][0]


# --------------------------------------------------------------------------- INTEG
@integ
def t_model_vol_ties_to_contributions():
    """Cube Model vol (book, HistFull) == /contributions vol_1d — same sigma, independent
    implementations (atoti array std vs numpy cov). Float-precision tolerance."""
    import requests
    cube = float(_book_cell("Model vol")["Model vol"])
    c = requests.get(f"{API}/contributions", params={"date": DATE}, timeout=60).json()
    assert abs(cube - c["vol_1d"]) < 5e-10, (cube, c["vol_1d"])


@integ
def t_model_vol_ties_to_whatif():
    """Cube Model vol == /whatif before.model_vol_1d (_risk_from_weights, third code path)."""
    import requests
    cube = float(_book_cell("Model vol")["Model vol"])
    w = requests.post(f"{API}/whatif", json={"trades": []}, timeout=120).json()
    assert abs(cube - w["before"]["model_vol_1d"]) < 5e-10, (cube, w["before"]["model_vol_1d"])


@integ
def t_model_vol_identity_in_cube():
    """In-cube identity: Model vol^2 == Scenario PnL vol^2 + Specific variance, book level."""
    r = _book_cell("Model vol,Scenario PnL vol,Specific variance")
    mv, sv, spv = float(r["Model vol"]), float(r["Scenario PnL vol"]), float(r["Specific variance"])
    assert abs(mv * mv - (sv * sv + spv)) < 1e-14, r
    assert mv > sv > 0                                    # specific adds something


@integ
def t_model_vol_drills_and_is_subadditive():
    """Per-sector Model vol: every cell positive, and the book vol <= the sum of sector vols
    (diversification — vol is sub-additive, unlike the additive marginal measures)."""
    j = _pivot(rows="Sector", measures="Model vol",
               filters={"Book": ["Soros"], "Date": [DATE], "ScenarioSet": ["HistFull"]})
    vols = [float(r["Model vol"]) for r in j["records"] if r.get("Model vol") is not None]
    assert len(vols) >= 5, f"expected sector drill, got {len(vols)} cells"
    assert all(v > 0 for v in vols)
    book = float(_book_cell("Model vol")["Model vol"])
    assert book <= sum(vols) + 1e-12, (book, sum(vols))


@integ
def t_model_vol_scenario_set_semantics():
    """Evt window vol is a different (regime) number from HistFull; the length-1 Hypo sets are
    degenerate (sample std undefined) and must read blank/NaN rather than a fake number."""
    hist = float(_book_cell("Model vol")["Model vol"])
    evt = _book_cell("Model vol", scen="Evt:COVID2020").get("Model vol")
    assert evt is not None and abs(float(evt) - hist) > 1e-6, (evt, hist)
    hypo = _book_cell("Model vol,Scenario n", scen="Hypo:MomentumCrash")
    assert int(hypo["Scenario n"]) == 1
    v = hypo.get("Model vol")
    ok_degenerate = v is None or (isinstance(v, float) and (v != v))   # None or NaN
    assert ok_degenerate, f"length-1 set should be degenerate, got {v}"


@integ
def t_model_vol_on_trends():
    """/trends serves Model vol (the vector-derived date-by-date path) — spot the series is
    positive and the latest point ties to the cube cell."""
    import requests
    j = requests.get(f"{API}/trends", params={"set": "HistFull", "measures": "Model vol"},
                     timeout=600).json()
    recs = [r for r in j["records"] if r.get("Model vol") is not None]
    assert len(recs) > 24, f"expected a long monthly series, got {len(recs)}"
    assert all(r["Model vol"] > 0 for r in recs)
    cube = float(_book_cell("Model vol")["Model vol"])
    assert abs(recs[-1]["Model vol"] - cube) < 1e-12, (recs[-1], cube)


@integ
def t_marginal_model_vol_is_euler():
    """Marginal Model vol is the Euler decomposition: sector marginals sum EXACTLY to the book
    Model vol, and '% of Model vol' sums to 100%."""
    j = _pivot(rows="Sector", measures="Marginal Model vol,% of Model vol",
               filters={"Book": ["Soros"], "Date": [DATE], "ScenarioSet": ["HistFull"]})
    marg = [float(r["Marginal Model vol"]) for r in j["records"]
            if r.get("Marginal Model vol") is not None]
    shares = [float(r["% of Model vol"]) for r in j["records"]
              if r.get("% of Model vol") is not None]
    book = float(_book_cell("Model vol")["Model vol"])
    assert abs(sum(marg) - book) < 1e-12, (sum(marg), book)
    assert abs(sum(shares) - 1.0) < 1e-12, sum(shares)


@integ
def t_marginal_model_vol_equals_ctr():
    """Per-NAME Marginal Model vol == /contributions CTR (w·(Σw)/σ) — the cube's array algebra
    against the numpy Euler decomposition, for the top-5 risk names."""
    import requests
    c = requests.get(f"{API}/contributions", params={"date": DATE}, timeout=60).json()
    j = _pivot(rows="Position", measures="Marginal Model vol",
               filters={"Book": ["Soros"], "Date": [DATE], "ScenarioSet": ["HistFull"]})
    cube = {r["Position"]: float(r["Marginal Model vol"]) for r in j["records"]
            if r.get("Marginal Model vol") is not None}
    for p in c["positions"][:5]:
        assert p["position"] in cube, p["position"]
        assert abs(cube[p["position"]] - p["ctr"]) < 5e-10, \
            (p["ticker"], cube[p["position"]], p["ctr"])


@integ
def t_incremental_model_vol_ties_to_whatif():
    """Incremental Model vol (vol released by removing the name) == the /whatif before→after
    model-vol delta when the same name is dropped — cube array algebra vs the numpy what-if
    engine, for the top risk name."""
    import requests
    c = requests.get(f"{API}/contributions", params={"date": DATE}, timeout=60).json()
    top = c["positions"][0]
    j = _pivot(rows="Position", measures="Incremental Model vol",
               filters={"Book": ["Soros"], "Date": [DATE], "ScenarioSet": ["HistFull"]})
    cube = {r["Position"]: float(r["Incremental Model vol"]) for r in j["records"]
            if r.get("Incremental Model vol") is not None}
    w = requests.post(f"{API}/whatif",
                      json={"trades": [{"position": top["position"], "weight": 0}]},
                      timeout=120).json()
    delta = w["before"]["model_vol_1d"] - w["after"]["model_vol_1d"]
    assert abs(cube[top["position"]] - delta) < 5e-10, (cube[top["position"]], delta)
    # sub-additivity sanity: releasing the top name frees LESS than its Euler contribution
    # would suggest is impossible — incremental positive, and below the book vol
    assert 0 < cube[top["position"]] < float(_book_cell("Model vol")["Model vol"])


@integ
def t_factor_return_vol_identity():
    """In-cube identity at every Factor member: Scenario PnL vol == |Net exposure| ·
    Factor return vol (std(x·f) = |x|·std(f)) — pins the raw-vol measure that /stress and
    /reverse_stress now consume via _factor_vols."""
    j = _pivot(rows="Factor", measures="Scenario PnL vol,Net exposure,Factor return vol",
               filters={"Book": ["Soros"], "Date": [DATE], "ScenarioSet": ["HistFull"]})
    checked = 0
    for r in j["records"]:
        if any(r.get(k) is None for k in ("Scenario PnL vol", "Net exposure", "Factor return vol")):
            continue
        lhs = float(r["Scenario PnL vol"])
        rhs = abs(float(r["Net exposure"])) * float(r["Factor return vol"])
        assert abs(lhs - rhs) < 1e-14, (r["Factor"], lhs, rhs)
        assert float(r["Factor return vol"]) > 0
        checked += 1
    assert checked >= 10, f"only {checked} factors checked"


@integ
def t_hedge_served_from_cube():
    """/hedge now serves the cube measures; the numpy _hedge_table cross-check (`verification`)
    must sit at float precision, and the D6 identity holds: the min-variance market hedge beats
    (or equals) full Market neutralization."""
    import requests
    j = requests.get(f"{API}/hedge", params={"date": DATE}, timeout=120).json()
    assert j.get("source") == "cube"
    v = j["verification"]
    assert v["vol_base_abs_diff"] < 5e-10, v
    assert v["max_vol_after_abs_diff"] < 5e-10, v
    assert v["h_star_abs_diff"] is not None and v["h_star_abs_diff"] < 5e-8, v
    mkt_row = next(r for r in j["rows"] if r["factor"] == "Market")
    assert j["market_hedge"]["vol_after"] <= mkt_row["vol_after"] + 1e-12
    # no factor hedge beats the specific floor
    assert all(r["vol_after"] >= j["specific_vol"] - 1e-12 for r in j["rows"])


@integ
def t_top5_risk_share_cube():
    """The tt.rank-based Top-5 risk share: the scalar equals the sum of the 5 largest
    '% of Total VaR 99' name shares from a by-Position pivot (validates the flat-hierarchy
    ranking wiring), sits in (0, 1], and reads via /limits."""
    import requests
    top5 = float(_book_cell("Top-5 risk share")["Top-5 risk share"])
    assert 0 < top5 <= 1, top5
    j = _pivot(rows="Position", measures="% of Total VaR 99",
               filters={"Book": ["Soros"], "Date": [DATE], "ScenarioSet": ["HistFull"]})
    shares = sorted((float(r["% of Total VaR 99"]) for r in j["records"]
                     if r.get("% of Total VaR 99") is not None), reverse=True)
    assert abs(top5 - sum(shares[:5])) < 1e-12, (top5, sum(shares[:5]))
    lim = requests.get(f"{API}/limits", timeout=60).json()
    lt = next(c for c in lim["checks"] if c["name"] == "Top-5 risk share")
    assert abs(float(lt["value"]) - top5) < 1e-12, (lt["value"], top5)


@integ
def t_gross_net_weight_measures():
    """Gross/Net weight measures: the in-cube identity Net weight == Net exposure at
    Factor=Market (unit Market loading makes x_Market = Σw), Gross >= |Net|, the 13F book is
    fully invested (net ≈ 1), and /whatif serves them from the cube with the numpy weights
    inside the tight verification bound."""
    import requests
    r = _book_cell("Net weight,Gross weight")
    net, gross = float(r["Net weight"]), float(r["Gross weight"])
    assert abs(net - 1.0) < 1e-9 and gross >= abs(net) - 1e-12, (net, gross)
    j = _pivot(rows="Factor", measures="Net exposure",
               filters={"Book": ["Soros"], "Date": [DATE], "ScenarioSet": ["HistFull"]})
    mkt = next(float(x["Net exposure"]) for x in j["records"] if x["Factor"] == "Market")
    assert abs(net - mkt) < 1e-12, (net, mkt)
    w = requests.post(f"{API}/whatif", json={"trades": []}, timeout=120).json()
    assert abs(w["before"]["net"] - net) < 1e-12
    assert "error" not in w["verification"] and w["verification"]["max_abs_diff_vols"] < 5e-10


@integ
def t_exceedance_rate_cube():
    """Exceedance rate 2s: recomputed in numpy from the /scenario_pnl path it must match at
    1e-12; the equity book reads fat (above the ~4.6% normal expectation is typical but not
    forced — assert a sane range); degenerate (blank) on the length-1 Hypo sets; drills by
    sector with every cell in [0, 1]."""
    import requests
    r = _book_cell("Exceedance rate 2s,Scenario PnL vol,Scenario n")
    rate = float(r["Exceedance rate 2s"])
    assert 0.005 < rate < 0.20, rate
    sp = requests.get(f"{API}/scenario_pnl",
                      params={"date": DATE, "set": "HistFull"}, timeout=60).json()
    pnl = [p["pnl"] for p in sp["points"]]
    import statistics
    sd = statistics.stdev(pnl)
    ref = sum(1 for v in pnl if v < -2 * sd or v > 2 * sd) / len(pnl)
    assert abs(rate - ref) < 1e-12, (rate, ref)
    hypo = _book_cell("Exceedance rate 2s", scen="Hypo:MomentumCrash").get("Exceedance rate 2s")
    assert hypo is None or (isinstance(hypo, float) and hypo != hypo), hypo
    j = _pivot(rows="Sector", measures="Exceedance rate 2s",
               filters={"Book": ["Soros"], "Date": [DATE], "ScenarioSet": ["HistFull"]})
    cells = [float(x["Exceedance rate 2s"]) for x in j["records"]
             if x.get("Exceedance rate 2s") is not None]
    assert len(cells) >= 5 and all(0 <= c <= 1 for c in cells)


@integ
def t_pit_sets_identities():
    """The PIT:* truncated-history sets: (a) hidden from /meta's main dropdown but served as
    pit_sets; (b) the LAST PIT set == HistFull (full panel) — Model vol identical; (c) at an
    EARLIER month t, Model vol under PIT:t differs from HistFull-at-t (the honest-as-of point)
    and the per-factor Scenario PnL vol under PIT:t ties a numpy std of the history ≤ t
    recomputed from /timeseries-free first principles via /scenario_pnl on HistFull."""
    import requests
    meta = requests.get(f"{API}/meta", timeout=30).json()
    assert meta["pit_sets"] and not any(s.startswith("PIT:") for s in meta["scenario_sets"])
    last_pit = meta["pit_sets"][-1]
    a = _book_cell("Model vol")
    b = _book_cell("Model vol", scen=last_pit)
    assert abs(float(a["Model vol"]) - float(b["Model vol"])) < 1e-15, (a, b)
    # earlier month: PIT vol must differ from the anachronistic full-history read
    t_mid = "2019-12-31"
    hist_mid = _pivot(rows="ScenarioSet", measures="Model vol",
                      filters={"Book": ["Soros"], "Date": [t_mid], "ScenarioSet": ["HistFull"]})
    pit_mid = _pivot(rows="ScenarioSet", measures="Model vol",
                     filters={"Book": ["Soros"], "Date": [t_mid],
                              "ScenarioSet": [f"PIT:{t_mid}"]})
    hv = float(hist_mid["records"][0]["Model vol"])
    pv = float(pit_mid["records"][0]["Model vol"])
    assert abs(hv - pv) > 1e-5, (hv, pv)      # 2019 PIT vol excludes COVID/2022 — must differ
    # per-factor PIT vol ties numpy: std of the HistFull daily book path ≤ t at the factor level
    sp = requests.get(f"{API}/scenario_pnl",
                      params={"date": t_mid, "set": "HistFull",
                              "filters": json.dumps({"Factor": ["Momentum"]})}, timeout=60).json()
    import statistics
    pnl = [(p["date"], p["pnl"]) for p in sp["points"]]
    upto = [v for d_, v in pnl if d_ <= t_mid]
    ref = statistics.stdev(upto)
    q = _pivot(rows="Factor", measures="Scenario PnL vol",
               filters={"Book": ["Soros"], "Date": [t_mid], "ScenarioSet": [f"PIT:{t_mid}"]})
    mom = next(float(r["Scenario PnL vol"]) for r in q["records"] if r["Factor"] == "Momentum")
    assert abs(mom - ref) < 5e-12, (mom, ref)


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
    else:
        print(f"SKIP: backend not reachable at {API}")
    print(f"\n{p} passed, {f} failed")
    raise SystemExit(1 if f else 0)


if __name__ == "__main__":
    main()
