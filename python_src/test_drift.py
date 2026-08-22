"""
test_drift.py — checks for the style-drift attribution (Phase 4; risk_api.py /drift +
barra_universe_drift.py).

  UNIT  — always run, no backend, no network: the pure attribution math — net portfolio exposure, the
          entered/exited/reweighted/loading_drift decomposition (sources sum to the total Δ exactly),
          and the pre/post drift summary ranking.
  INTEG — need the live backend on :8010 AND the built artifact; SKIP if down: /drift returns a
          per-factor net-exposure series, a summary ranked by |Δ| whose four sources reconcile to Δ,
          and a per-factor 'lean'.

Run:  BARRA_API=http://127.0.0.1:8010 ../barra/bin/python test_drift.py
"""
from __future__ import annotations
import os

import pandas as pd

import barra_universe_drift as ud

API = os.environ.get("BARRA_API", "http://127.0.0.1:8010")

# The per-manager precompute sweep gave every loaded manager its own <stem>.<Manager>.parquet, so
# `_resolve_artifact` serves those directly and only an UNBUILT manager reaches `_manager_guard`.
# A name no precompute can have run for is the reliable way to exercise the guard; it is also
# the real case it exists for (a UI asking for a manager whose precompute has not been run).
_UNBUILT = "NoSuchManager"


def _managers_with_own_artifact(stem: str, limit: int = 2) -> list:
    """Up to `limit` loaded managers (never the default) that have their own artifact on disk.
    Empty when only the default manager is built — the caller's loop then simply does nothing,
    which is the correct behaviour on a single-manager build."""
    import pathlib
    import requests
    out = pathlib.Path(__file__).resolve().parent.parent / "data"
    j = requests.get(f"{API}/meta", timeout=60).json()
    managers = sorted(m["manager"] for m in (j.get("managers") or []) if m["manager"] != "Soros")
    return [mgr for mgr in managers if (out / f"{stem}.{mgr}.parquet").exists()][:limit]

UNIT, INTEG = [], []


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


# ----------------------------------------------------------------------------- UNIT
@unit
def t_portfolio_exposure():
    w = {"A": 0.5, "B": 0.5}
    L = {"A": {"Size": 2.0}, "B": {"Size": -1.0}}
    assert abs(ud.portfolio_exposure(w, L, ["Size"])["Size"] - 0.5) < 1e-9


@unit
def t_decompose_sources_sum_to_delta():
    # t0: A,B held; t1: A resized + drifted, B dropped, C entered
    w0 = {"A": 0.5, "B": 0.5}
    l0 = {"A": {"Size": 2.0}, "B": {"Size": 0.0}}
    w1 = {"A": 0.7, "C": 0.3}
    l1 = {"A": {"Size": 1.0}, "C": {"Size": -3.0}}
    a = ud.decompose(w0, l0, w1, l1, ["Size"])["Size"]
    x0 = ud.portfolio_exposure(w0, l0, ["Size"])["Size"]
    x1 = ud.portfolio_exposure(w1, l1, ["Size"])["Size"]
    assert abs(a["delta"] - (x1 - x0)) < 1e-9, a
    assert abs((a["entered"] + a["exited"] + a["reweighted"] + a["loading_drift"]) - a["delta"]) < 1e-9
    # C entered with -3 loading at weight .3 -> -0.9
    assert abs(a["entered"] - (-0.9)) < 1e-9
    # B exited (loading 0) -> 0; A loading drift: w1*(1-2)=0.7*-1=-0.7
    assert abs(a["loading_drift"] - (-0.7)) < 1e-9


@unit
def t_drift_summary_ranks_by_abs_delta():
    idx = pd.to_datetime(["2019-12-31", "2020-12-31", "2021-12-31", "2022-12-31"])
    series = pd.DataFrame({"Size": [0, 0, -1, -1], "Beta": [0, 0, 0.2, 0.2]}, index=idx)
    s = ud.drift_summary(series, pd.Timestamp("2021-01-01"))
    assert list(s.index)[0] == "Size"                       # |Δ|=1 ranks above Beta |Δ|=0.2
    assert abs(s.loc["Size", "delta"] - (-1.0)) < 1e-9


# ----------------------------------------------------------------------------- INTEG
@integ
def t_drift_series_and_summary():
    import requests
    j = requests.get(f"{API}/drift", timeout=60).json()
    assert j["factors"] == ud.STYLE and j["sources"] == ud.SOURCES
    assert j["series"] and all("month" in r for r in j["series"])
    deltas = [abs(r["delta"]) for r in j["summary"]]
    assert deltas == sorted(deltas, reverse=True), "summary not ranked by |delta|"
    for r in j["summary"]:
        src = sum(r[f"src_{s}"] for s in ud.SOURCES)
        assert abs(src - r["delta"]) < 1e-6, r                # four sources reconcile to delta
        assert "lean" in r


@integ
def t_drift_accepts_split():
    import requests
    j = requests.get(f"{API}/drift", params={"split": "2022-01-01"}, timeout=60).json()
    assert j["split"] == "2022-01-01" and j["t1"] >= j["t0"]


@integ
def t_drift_manager_guard():
    """Default manager (Soros) is UNCHANGED; a manager with its own artifact is served from it
    (and the live attribution below it is scoped to that manager); a manager with NO artifact
    comes back as a clean manager_mismatch status.

    Two branches, because `_resolve_artifact` has two: a manager with its OWN
    `<stem>.<Manager>.parquet` is served from it (every loaded manager has one since the
    124-manager precompute sweep), and a manager with NO artifact falls to `_manager_guard`,
    which refuses to serve the legacy file under another manager's label. This test used to
    assert that ANY non-default manager was a mismatch — true when only Soros had an artifact,
    stale since. `_UNBUILT` is a name no precompute can have run for, which is exactly the state
    the guard exists to catch.
    """
    import requests
    base = requests.get(f"{API}/drift", timeout=60).json()
    assert "status" not in base and base["series"], base
    for mgr in _managers_with_own_artifact("universe_drift"):
        own = requests.get(f"{API}/drift", params={"manager": mgr}, timeout=60).json()
        assert "status" not in own and own["series"], (mgr, own)
        # the live half (portfolio_at -> decompose) must be scoped too: a different manager cannot
        # return the default manager's net exposures (the /whatchanged bug, 2026-08-21)
        assert own["summary"] != base["summary"], mgr
    mism = requests.get(f"{API}/drift", params={"manager": _UNBUILT}, timeout=60).json()
    assert mism["status"] == "manager_mismatch", mism
    assert mism["requested_manager"] == _UNBUILT and mism["artifact_manager"] == "Soros", mism
    assert mism["kind"] == "drift" and mism["reason"], mism


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
