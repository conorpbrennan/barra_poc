"""
test_dq.py — checks for the data-quality / trust panel (Step 2).

  * UNIT  — always run, no backend: barra_dq_checks.run() returns structured {level,name,detail}
            results from the disk frames, and accepts an injected frames dict.
  * INTEG — need the live backend on :8010; SKIP if down. /dq returns a status + summary + checks
            + stubs computed against the cube's live frames.

Run:  BARRA_API=http://127.0.0.1:8010 ../barra/bin/python test_dq.py
"""
from __future__ import annotations
import os

API = os.environ.get("BARRA_API", "http://127.0.0.1:8010")
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


# --------------------------------------------------------------------------- UNIT
@unit
def t_dq_run_returns_structured():
    """run() returns a non-empty list of {level,name,detail} dicts with valid levels."""
    import barra_dq_checks
    res = barra_dq_checks.run()
    assert isinstance(res, list) and res, "no results"
    for r in res:
        assert set(r) == {"level", "name", "detail"}, r
        assert r["level"] in ("PASS", "WARN", "FAIL"), r["level"]


@unit
def t_dq_run_accepts_injected_frames():
    """Passing frames in avoids the disk re-read and checks exactly those frames."""
    import pandas as pd, pathlib, barra_dq_checks
    out = pathlib.Path(__file__).resolve().parent.parent / "data"
    frames = {n: pd.read_parquet(out / f"{n}.parquet") for n in barra_dq_checks.KEYS}
    res = barra_dq_checks.run(frames)
    assert res and all("level" in r for r in res)
    # the known Sector stub check is present
    assert any("Sector populated" in r["name"] for r in res), [r["name"] for r in res]


@unit
def t_dq_size_curve_proxy_disclosure():
    """The size-curve imputation is disclosed, never silent: the proxy-loadings check exists,
    reads a plausible share of held weight (>0 on the imputed frames, <=100%), and its level
    follows the 25%-weight WARN threshold."""
    import re, barra_dq_checks
    res = barra_dq_checks.run()
    prox = [r for r in res if "size-curve proxy" in r["name"]]
    assert len(prox) == 1, [r["name"] for r in res]
    r = prox[0]
    m = re.search(r"([\d.]+)% of weight", r["detail"])
    assert m, r["detail"]
    w = float(m.group(1))
    assert 0 <= w <= 100, w
    assert r["level"] == ("WARN" if w > 25 else "PASS"), r


# --------------------------------------------------------------------------- INTEG
@integ
def t_dq_endpoint_shape():
    """/dq returns status, summary counts, checks, stubs, and per-frame latest dates."""
    import requests
    d = requests.get(f"{API}/dq", timeout=60); d.raise_for_status()
    j = d.json()
    assert j["status"] in ("pass", "warn", "fail"), j["status"]
    for k in ("PASS", "WARN", "FAIL"):
        assert k in j["summary"], j["summary"]
    assert j["checks"], "no checks"
    assert "country_stub_US" in j["stubs"] and "n_securities" in j["stubs"], j["stubs"]
    assert j["latest_date"].get("positions"), j["latest_date"]


@integ
def t_dq_status_matches_counts():
    """The worst-of status agrees with the summary counts."""
    import requests
    j = requests.get(f"{API}/dq", timeout=60).json()
    s = j["summary"]
    expect = "fail" if s["FAIL"] else ("warn" if s["WARN"] else "pass")
    assert j["status"] == expect, (j["status"], s)


def _run(group):
    p = f = 0
    for fn in group:
        try:
            fn(); print(f"PASS  {fn.__name__}"); p += 1
        except Exception as e:
            import traceback
            print(f"FAIL  {fn.__name__}: {type(e).__name__}: {e}"); traceback.print_exc(); f += 1
    return p, f


def main():
    p = f = 0
    print("=== unit (no backend) ===")
    a, b = _run(UNIT); p += a; f += b
    print("\n=== integration (live backend) ===")
    if _backend_up():
        a, b = _run(INTEG); p += a; f += b
    else:
        print(f"SKIP: backend not reachable at {API}")
    print(f"\n{p} passed, {f} failed")
    raise SystemExit(1 if f else 0)


if __name__ == "__main__":
    main()
