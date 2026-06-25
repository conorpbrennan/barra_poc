"""
test_whatchanged.py — checks for the "what changed" QoQ diff + commentary (Step 9; risk_api.py
/whatchanged + /whatchanged/analysis).

  UNIT  — always run, no backend, no tokens: _prior_filing_date picks the previous distinct 13F book.
  INTEG — need the live backend on :8010; SKIP if down: /whatchanged returns from<to, the position
          in/out/resized split, a factor-exposure attribution whose four sources reconcile to Δ, and a
          before/after/delta risk block; an explicit ?prev= is honoured.
  LIVE  — opt-in only (RUN_LLM=1): actually stream /whatchanged/analysis and check it returns markdown.

Run:  BARRA_API=http://127.0.0.1:8010 ../barra/bin/python test_whatchanged.py
      RUN_LLM=1 BARRA_API=... ../barra/bin/python test_whatchanged.py   # include the live call
"""
from __future__ import annotations
import os

API = os.environ.get("BARRA_API", "http://127.0.0.1:8010")
UNIT, INTEG, LIVE = [], [], []


def unit(fn):
    UNIT.append(fn); return fn


def integ(fn):
    INTEG.append(fn); return fn


def live(fn):
    LIVE.append(fn); return fn


def _backend_up():
    try:
        import requests
        return requests.get(f"{API}/dims", timeout=5).status_code == 200
    except Exception:
        return False


# ----------------------------------------------------------------------------- UNIT
@unit
def t_prior_filing_date():
    import pandas as pd
    import risk_api
    # monthly as-of book: {A,B} held Mar & Apr (same filing), {A,C} from Jun (new filing)
    rows = ([{"Book": "Soros", "Date": pd.Timestamp("2024-03-31"), "Position": p} for p in "AB"]
            + [{"Book": "Soros", "Date": pd.Timestamp("2024-04-30"), "Position": p} for p in "AB"]
            + [{"Book": "Soros", "Date": pd.Timestamp("2024-06-30"), "Position": p} for p in "AC"])
    bpos = pd.DataFrame(rows)
    prev = risk_api._prior_filing_date(bpos, pd.Timestamp("2024-06-30"))
    assert prev == pd.Timestamp("2024-04-30"), prev          # latest date whose set != {A,C}
    # no earlier filing -> None
    assert risk_api._prior_filing_date(bpos, pd.Timestamp("2024-03-31")) is None


# ----------------------------------------------------------------------------- INTEG
@integ
def t_whatchanged_shape_and_reconcile():
    import requests
    j = requests.get(f"{API}/whatchanged", timeout=60).json()
    assert j["from"] < j["to"], (j["from"], j["to"])
    p = j["positions"]
    for k in ("entered", "exited", "resized"):
        assert isinstance(p[k], list)
    assert p["n_before"] >= 1 and p["n_after"] >= 1
    # the four attribution sources reconcile to delta per factor
    for r in j["exposure_attribution"]:
        src = r["src_entered"] + r["src_exited"] + r["src_reweighted"] + r["src_loading_drift"]
        assert abs(src - r["delta"]) < 1e-6, r
        assert abs((r["after"] - r["before"]) - r["delta"]) < 1e-6, r
    rk = j["risk"]
    for k in ("scenario_var_99", "total_var_99", "specific_vol"):
        assert "before" in rk[k] and "after" in rk[k] and "delta" in rk[k], rk[k]
        assert rk[k]["before"] is not None and rk[k]["after"] is not None


@integ
def t_whatchanged_accepts_prev():
    import requests
    j = requests.get(f"{API}/whatchanged", timeout=60).json()
    # ask for the same `to` but pin `prev` to the existing `from` — should round-trip
    j2 = requests.get(f"{API}/whatchanged", params={"date": j["to"], "prev": j["from"]},
                      timeout=60).json()
    assert j2["from"] == j["from"] and j2["to"] == j["to"], (j2["from"], j2["to"])


# ----------------------------------------------------------------------------- LIVE (opt-in)
@live
def t_whatchanged_analysis_streams_markdown():
    import requests
    r = requests.post(f"{API}/whatchanged/analysis", json={"notes": None}, stream=True, timeout=180)
    r.raise_for_status(); r.encoding = "utf-8"
    text = "".join(c for c in r.iter_content(chunk_size=None, decode_unicode=True) if c)
    assert len(text) > 80, f"expected a markdown read, got {len(text)} chars"


def main():
    p = f = 0
    print("=== unit (no backend) ===")
    for fn in UNIT:
        try:
            fn(); print(f"PASS  {fn.__name__}"); p += 1
        except Exception as e:
            import traceback
            print(f"FAIL  {fn.__name__}: {type(e).__name__}: {e}"); traceback.print_exc(); f += 1
    print("\n=== integration (live backend) ===")
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
            print("\nSKIP live LLM (set RUN_LLM=1 to stream /whatchanged/analysis)")
    else:
        print(f"SKIP: backend not reachable at {API}")
    print(f"\n{p} passed, {f} failed")
    raise SystemExit(1 if f else 0)


if __name__ == "__main__":
    main()
