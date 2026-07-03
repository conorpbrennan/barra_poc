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
