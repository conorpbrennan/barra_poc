"""
test_whatif.py — checks for pre-trade / what-if (Step 6).

  * INTEG — need the live backend on :8010; SKIP if down. The numpy risk reproduction matches the
            cube's reported figures ("before"), a no-op is a zero delta, dropping/resizing move
            gross/net and HHI the right way, and bad inputs 400.

The risk math reads the cube + frames, so there are no pure-unit cases.

Run:  BARRA_API=http://127.0.0.1:8010 ../barra/bin/python test_whatif.py
"""
from __future__ import annotations
import os

API = os.environ.get("BARRA_API", "http://127.0.0.1:8010")
INTEG = []


def integ(fn):
    INTEG.append(fn); return fn


def _backend_up():
    try:
        import requests
        return requests.get(f"{API}/dims", timeout=5).status_code == 200
    except Exception:
        return False


def _whatif(trades):
    import requests
    r = requests.post(f"{API}/whatif", json={"trades": trades}, timeout=60)
    r.raise_for_status()
    return r.json()


@integ
def t_whatif_before_matches_cube():
    """The numpy 'before' reproduces the cube's reported risk (HistFull) to a tight tolerance —
    proves the what-if engine is faithful, so deltas are trustworthy."""
    import requests
    j = _whatif([])
    b = j["before"]
    cube = requests.get(f"{API}/risk", params={"date": j["date"], "set": "HistFull"}, timeout=30).json()
    assert abs(b["scenario_var_99"] - cube["factor_var"]) < 1e-3, (b["scenario_var_99"], cube["factor_var"])
    assert abs(b["total_var_99"] - cube["total_var"]) < 1e-3, (b["total_var_99"], cube["total_var"])
    assert abs(b["specific_vol"] - cube["specific_vol"]) < 1e-3, (b["specific_vol"], cube["specific_vol"])


@integ
def t_whatif_noop_zero_delta():
    """Empty trades -> after == before, every delta ~0, and the holdings list is returned."""
    j = _whatif([])
    assert j["holdings"], "no holdings returned"
    for k, v in j["delta"].items():
        assert v is None or abs(v) < 1e-12, (k, v)


@integ
def t_whatif_holdings_sorted():
    """Holdings come back sorted by weight, descending, summing to ~1 (long-only overlay)."""
    j = _whatif([])
    ws = [h["weight"] for h in j["holdings"]]
    assert ws == sorted(ws, reverse=True), "holdings not weight-sorted"
    assert abs(sum(ws) - 1.0) < 1e-6, sum(ws)


@integ
def t_whatif_drop_reduces_gross_net():
    """Dropping the largest holding reduces gross and net by ~its weight."""
    j = _whatif([])
    top = j["holdings"][0]
    res = _whatif([{"position": top["position"], "weight": 0.0}])
    assert res["delta"]["net"] < 0 and res["delta"]["gross"] < 0, res["delta"]
    assert abs(res["delta"]["net"] + top["weight"]) < 1e-6, (res["delta"]["net"], top["weight"])


@integ
def t_whatif_concentrating_raises_top5_share():
    """Doubling the top name (more concentrated) raises the top-5 risk share (the ch-09 CTR
    concentration idiom that replaced Risk HHI)."""
    j = _whatif([])
    top = j["holdings"][0]
    res = _whatif([{"position": top["position"], "weight": 2 * top["weight"]}])
    assert res["after"]["top5_ctr_share"] > res["before"]["top5_ctr_share"], \
        (res["before"]["top5_ctr_share"], res["after"]["top5_ctr_share"])
    assert 0 < res["before"]["top5_ctr_share"] <= 1


@integ
def t_whatif_add_universe_name():
    """A coverage-universe name not currently held can be ADDED: /whatif returns the universe, and
    adding one with a target weight raises net by ~that weight and appears in the trades."""
    j = _whatif([])
    held = {h["position"] for h in j["holdings"]}
    uni = j.get("universe", [])
    assert uni, "no universe returned"
    new = next(u for u in uni if u["position"] not in held)
    res = _whatif([{"position": new["position"], "weight": 0.03}])
    assert abs(res["delta"]["net"] - 0.03) < 1e-6, res["delta"]["net"]
    assert any(t["position"] == new["position"] and t["old"] == 0.0 for t in res["trades"]), res["trades"]


@integ
def t_whatif_rejects_unknown_position():
    import requests
    r = requests.post(f"{API}/whatif", json={"trades": [{"position": "NOPE", "weight": 0.1}]}, timeout=30)
    assert r.status_code == 400, r.status_code


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
