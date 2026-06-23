"""
test_trends.py — checks for the time-series / trend endpoint (Step 4).

  * INTEG — need the live backend on :8010; SKIP if down. /trends returns book measures over the
            whole calendar (date-by-date, no OOM), the factor-breakdown mode works, and bad inputs
            are rejected.

There are no pure-unit cases here — /trends is a thin cube query; its logic is the cube's.

Run:  BARRA_API=http://127.0.0.1:8010 ../barra/bin/python test_trends.py
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


@integ
def t_trends_book_series_over_calendar():
    """Default /trends returns a multi-year book series with VaR/ES/HHI on each date."""
    import requests
    j = requests.get(f"{API}/trends", timeout=120).json()
    recs = j["records"]
    assert len(recs) > 50, f"expected a multi-year monthly series, got {len(recs)}"
    first = recs[0]
    for k in ("Date", "Scenario VaR 99", "Scenario ES 97.5", "Risk HHI"):
        assert k in first, (k, first)
    # dates are sorted ascending
    dates = [r["Date"] for r in recs]
    assert dates == sorted(dates), "records not sorted by date"


@integ
def t_trends_factor_breakdown():
    """`by=Factor` returns a Date x Factor breakdown of Net exposure (additive, single query)."""
    import requests
    j = requests.get(f"{API}/trends", params={"measures": "Net exposure", "by": "Factor"}, timeout=120).json()
    recs = j["records"]
    assert recs and "Factor" in recs[0] and "Net exposure" in recs[0], recs[:1]
    # Market carries the structural ~1.0 loading
    mkt = [r for r in recs if r.get("Factor") == "Market"]
    assert mkt and abs(mkt[0]["Net exposure"] - 1.0) < 0.2, mkt[:1]


@integ
def t_trends_rejects_bad_measure():
    """An off-allowlist measure -> 400 (same guard as /pivot)."""
    import requests
    r = requests.get(f"{API}/trends", params={"measures": "Not A Measure"}, timeout=30)
    assert r.status_code == 400, r.status_code


@integ
def t_trends_rejects_bad_by():
    """An unknown `by` dimension -> 400."""
    import requests
    r = requests.get(f"{API}/trends", params={"by": "NotADim"}, timeout=30)
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
