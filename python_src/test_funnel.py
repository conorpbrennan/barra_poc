"""
test_funnel.py — checks for the estimation-universe filtration funnel (Phase 2; risk_api.py /funnel +
barra_universe_funnel.py).

  UNIT  — always run, no backend, no network: the pure filter logic — first-failing-stage verdicts at
          their boundaries, missing-metric routing to "data unavailable" (not a filter drop),
          stability-buffer hysteresis, and funnel-count reconciliation.
  INTEG — need the live backend on :8010 AND the built artifact; SKIP if down: /funnel returns a
          per-month series where survivors ≤ population and counts reconcile, a latest waterfall, and a
          drop list whose every row carries a real filter stage.

Run:  BARRA_API=http://127.0.0.1:8010 ../barra/bin/python test_funnel.py
"""
from __future__ import annotations
import os

import numpy as np
import pandas as pd

import barra_universe_funnel as uf

API = os.environ.get("BARRA_API", "http://127.0.0.1:8010")
UNIT, INTEG = [], []
CFG = {"min_mcap": 1e8, "min_hist_days": 252, "min_adv": 1e6, "min_trade_freq": 0.9,
       "min_descriptors": 6, "allowed_sec_types": ["Common"]}


def unit(fn):
    UNIT.append(fn); return fn


def integ(fn):
    INTEG.append(fn); return fn


def _backend_up():
    try:
        import requests
        return requests.get(f"{API}/dims", timeout=5).status_code == 200
    except Exception:
        return False


def _ok():
    return {"sec_type": "Common", "mcap": 5e9, "hist_days": 600, "trade_freq": 0.99,
            "adv": 5e7, "n_descriptors": 9}


# ----------------------------------------------------------------------------- UNIT
@unit
def t_survives_clean_name():
    assert uf.first_failing_stage(_ok(), CFG) is None


@unit
def t_stage_order_and_boundaries():
    assert uf.first_failing_stage({**_ok(), "sec_type": "ADR"}, CFG) == "listing"
    assert uf.first_failing_stage({**_ok(), "mcap": 5e7}, CFG) == "size"           # below 1e8
    assert uf.first_failing_stage({**_ok(), "hist_days": 251}, CFG) == "history"   # below 252
    assert uf.first_failing_stage({**_ok(), "trade_freq": 0.5}, CFG) == "trading frequency"
    assert uf.first_failing_stage({**_ok(), "adv": 5e5}, CFG) == "liquidity"       # below 1e6
    assert uf.first_failing_stage({**_ok(), "n_descriptors": 5}, CFG) == "completeness"
    # boundary: exactly at the threshold passes
    assert uf.first_failing_stage({**_ok(), "mcap": 1e8, "hist_days": 252,
                                   "adv": 1e6, "n_descriptors": 6, "trade_freq": 0.9}, CFG) is None


@unit
def t_missing_metric_is_data_unavailable_not_a_filter_drop():
    assert uf.first_failing_stage({**_ok(), "mcap": np.nan}, CFG) == "data unavailable"
    assert uf.first_failing_stage({**_ok(), "mcap": None}, CFG) == "data unavailable"
    assert uf.first_failing_stage({**_ok(), "hist_days": 0}, CFG) == "data unavailable"


@unit
def t_buffer_hysteresis():
    # enter 55th, exit 50th: a clears enter; b is held by prior membership + above exit; c drops
    keep = uf.buffer_members({"a": 60.0, "b": 52.0, "c": 40.0}, prev={"b"}, enter=55, exit=50)
    assert keep == {"a", "b"}, keep
    # b would NOT enter fresh (no prior membership)
    keep2 = uf.buffer_members({"b": 52.0}, prev=set(), enter=55, exit=50)
    assert keep2 == set(), keep2


@unit
def t_held_positions_scoped_to_one_book():
    """Multi-manager Phase 3: `_held_positions` (which `run()`'s held_map now delegates to) must
    only count the REQUESTED book's holdings, not every manager's. Regression guard for the
    pre-fix bug where `held_map` had no Book filter at all -- with >1 book that silently meant
    'held by ANY manager'."""
    pos = pd.DataFrame({
        "Date": pd.to_datetime(["2024-01-31", "2024-01-31", "2024-01-31", "2024-02-29"]),
        "Book": ["Soros", "Soros", "Bridgewater", "Soros"],
        "Position": ["AAA", "BBB", "CCC", "AAA"],
    })
    soros_held = uf._held_positions(pos, "Soros")
    bw_held = uf._held_positions(pos, "Bridgewater")
    any_held = uf._held_positions(pos, None)
    assert soros_held == {(pd.Timestamp("2024-01-31"), "AAA"), (pd.Timestamp("2024-01-31"), "BBB"),
                          (pd.Timestamp("2024-02-29"), "AAA")}, soros_held
    assert bw_held == {(pd.Timestamp("2024-01-31"), "CCC")}, bw_held
    assert ("CCC" in {p for _, p in soros_held}) is False      # Soros never sees Bridgewater's name
    assert any_held == soros_held | bw_held                    # None = the old (buggy) any-book union
    assert any_held != soros_held                               # proves the two really differ


@unit
def t_run_book_param_default_matches_single_book_behaviour():
    """`run`'s new `book="Soros"` default must reproduce the OLD unfiltered behaviour exactly on
    single-book data (today's production positions.parquet has only ever held "Soros") — i.e. the
    Phase-3 fix is a no-op for today's data, only a no-op-that-becomes-necessary once a second book
    exists."""
    pos_single_book = pd.DataFrame({
        "Date": pd.to_datetime(["2024-01-31", "2024-02-29"]),
        "Book": ["Soros", "Soros"],
        "Position": ["AAA", "BBB"],
    })
    assert uf._held_positions(pos_single_book, "Soros") == uf._held_positions(pos_single_book, None)


@unit
def t_funnel_counts_reconcile():
    d = pd.DataFrame({
        "stage_dropped": ["size", "history", "data unavailable", None, None, "stability buffer"],
        "survived": [False, False, False, True, True, False],
        "held": [False, False, False, True, False, False],
    })
    fc = uf.funnel_counts(d, CFG)
    assert fc["population"] == 6 and fc["survivors"] == 2 and fc["held_survivors"] == 1
    assert fc["data_unavailable"] == 1
    total = fc["survivors"] + fc["data_unavailable"] + sum(fc["drops"].values())
    assert total == fc["population"], (total, fc)


# ----------------------------------------------------------------------------- INTEG
@integ
def t_funnel_series_reconciles():
    import requests
    j = requests.get(f"{API}/funnel", timeout=60).json()
    assert j["stages"] == uf.STAGES, j["stages"]
    assert j["series"], "empty series"
    for r in j["series"]:
        drops = sum(r[f"drop:{s}"] for s in uf.STAGES)
        assert r["survivors"] <= r["population"]
        assert r["survivors"] + r["data_unavailable"] + drops == r["population"], r


@integ
def t_funnel_latest_and_droplist():
    import requests
    j = requests.get(f"{API}/funnel", timeout=60).json()
    lat = j["latest"]
    assert 0 <= lat["survivors"] <= lat["population"]
    for d in j["dropped"]:
        assert d["stage_dropped"] in uf.STAGES, d           # only genuine filter drops listed
        assert "issuer" in d and "ticker" in d


@integ
def t_funnel_accepts_date():
    import requests
    series = requests.get(f"{API}/funnel", timeout=60).json()["series"]
    d = series[0]["month"]
    j = requests.get(f"{API}/funnel", params={"date": d}, timeout=60).json()
    assert j["selected_date"] == d


@integ
def t_funnel_book_guard():
    """Multi-manager Phase 3: the artifact is single-book. Default book (Soros, today's covered
    book) is UNCHANGED (normal series shape, no status field); any other book comes back as a
    clean book_mismatch status instead of silently wrong data."""
    import requests
    base = requests.get(f"{API}/funnel", timeout=60).json()
    assert "status" not in base and base["series"], base           # unchanged for the covered book
    mism = requests.get(f"{API}/funnel", params={"book": "Bridgewater"}, timeout=60).json()
    assert mism["status"] == "book_mismatch", mism
    assert mism["requested_book"] == "Bridgewater" and mism["artifact_book"] == "Soros", mism
    assert mism["reason"], mism


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
    else:
        print(f"SKIP: backend not reachable at {API}")
    print(f"\n{p} passed, {f} failed")
    raise SystemExit(1 if f else 0)


if __name__ == "__main__":
    main()
