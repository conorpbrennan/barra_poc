"""
test_universe.py — checks for the universe index-membership diagnostic (Phase 1; risk_api.py
/universe + barra_universe_membership.py).

  UNIT  — always run, no backend, no network: the pure classification logic — ticker canonicalize,
          S&P 500 PIT change-log parse + as-of membership, issuer-name normalize, bucketing, and
          weight aggregation.
  INTEG — need the live backend on :8010 AND the built artifact; SKIP if down: /universe returns a
          weight-by-bucket series whose buckets sum to ~1.0 per filing, a latest split with the
          'outside S&P 1500' headline in [0,1], and a sane Outside/Unclassified detail list.

Run:  BARRA_API=http://127.0.0.1:8010 ../barra/bin/python test_universe.py
"""
from __future__ import annotations
import os

import pandas as pd

import barra_universe_membership as um

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


# ----------------------------------------------------------------------------- UNIT
@unit
def t_canon():
    assert um.canon("BRK.B") == "BRK-B"
    assert um.canon("brk-b") == "BRK-B"
    assert um.canon("  aapl ") == "AAPL"
    assert um.canon(None) == "" and um.canon("") == ""


@unit
def t_parse_sp500_history():
    raw = b'date,tickers\n2020-01-01,"AAA,BBB"\n2020-06-01,"AAA,CCC"\n'
    h = um.parse_sp500_history(raw)
    assert [d.date().isoformat() for d, _ in h] == ["2020-01-01", "2020-06-01"], h
    assert h[0][1] == frozenset({"AAA", "BBB"})
    assert h[1][1] == frozenset({"AAA", "CCC"})


@unit
def t_sp500_member_asof():
    h = um.parse_sp500_history(b'date,tickers\n2020-01-01,"AAA,BBB"\n2020-06-01,"AAA,CCC"\n')
    assert um.sp500_member_asof(h, "AAA", pd.Timestamp("2020-03-01")) is True   # in first snapshot
    assert um.sp500_member_asof(h, "BBB", pd.Timestamp("2020-09-01")) is False  # dropped by 2nd snapshot
    assert um.sp500_member_asof(h, "CCC", pd.Timestamp("2020-09-01")) is True   # added in 2nd snapshot
    assert um.sp500_member_asof(h, "CCC", pd.Timestamp("2019-12-01")) is False  # before any snapshot
    assert um.sp500_member_asof(h, "AAA", pd.Timestamp("2020-06-01")) is True   # boundary is inclusive
    assert um.sp500_member_asof(h, "", pd.Timestamp("2020-03-01")) is False     # no ticker


@unit
def t_norm_name():
    assert um.norm_name("LINDE PLC") == "LINDE"
    assert um.norm_name("Hologic, Inc.") == "HOLOGIC"
    assert um.norm_name("Sealed Air Corp New") == "SEALED AIR"
    assert um.norm_name("AT&T Inc.") == "AT T"      # punctuation -> space, both tokens kept


@unit
def t_bucket_of():
    assert um.bucket_of(False, False, False) == "Unclassified"   # unresolved never 'Outside'
    assert um.bucket_of(True, True, False) == "S&P 500"          # PIT wins
    assert um.bucket_of(True, False, True) == "S&P 400/600"      # resolved, current-1500, not PIT
    assert um.bucket_of(True, False, False) == "Outside S&P 1500"


@unit
def t_aggregate():
    detail = pd.DataFrame({
        "report_date": pd.to_datetime(["2024-03-31"] * 3 + ["2024-06-30"] * 2),
        "bucket": ["S&P 500", "S&P 500", "Outside S&P 1500", "S&P 500", "Unclassified"],
        "weight": [0.5, 0.3, 0.2, 0.7, 0.3],
    })
    g = um.aggregate(detail)
    q1 = g[g["report_date"] == pd.Timestamp("2024-03-31")]
    assert abs(q1["weight"].sum() - 1.0) < 1e-9                  # weights conserved
    sp500 = q1[q1["bucket"] == "S&P 500"]
    assert abs(float(sp500["weight"].iloc[0]) - 0.8) < 1e-9 and int(sp500["n_names"].iloc[0]) == 2


# ----------------------------------------------------------------------------- INTEG
@integ
def t_universe_series_and_headline():
    import requests
    j = requests.get(f"{API}/universe", timeout=60).json()
    assert j["buckets"] == um.BUCKETS, j["buckets"]
    series = j["series"]
    assert series and all("report_date" in r for r in series), series[:1]
    # every filing's bucket weights sum to ~1.0 (each name in exactly one bucket, weights sum to 1)
    last = series[-1]
    tot = sum(last[b] for b in um.BUCKETS)
    assert abs(tot - 1.0) < 1e-6, (tot, last)
    lat = j["latest"]
    assert 0.0 <= lat["outside_sp1500"] <= 1.0 and 0.0 <= lat["unclassified"] <= 1.0, lat


@integ
def t_universe_detail_is_outside_or_unclassified():
    import requests
    j = requests.get(f"{API}/universe", timeout=60).json()
    for d in j["detail"]:
        assert d["bucket"] in ("Outside S&P 1500", "Unclassified"), d
        assert "issuer" in d and "weight" in d
    # detail is sorted by weight descending
    ws = [d["weight"] for d in j["detail"]]
    assert ws == sorted(ws, reverse=True), ws[:5]


@integ
def t_universe_accepts_date():
    import requests
    series = requests.get(f"{API}/universe", timeout=60).json()["series"]
    d = series[0]["report_date"]
    j = requests.get(f"{API}/universe", params={"date": d}, timeout=60).json()
    assert j["selected_date"] == d and j["latest"]["report_date"] == d


@integ
def t_universe_book_guard():
    """Multi-manager Phase 3: barra_universe_membership.py hardcodes SOROS_CIK, so its artifact
    ALWAYS covers Soros regardless of what's in positions.parquet. Default book (Soros) is
    UNCHANGED (normal series shape); any other book comes back as a clean book_mismatch status
    rather than silently showing Soros's membership split under another manager's label."""
    import requests
    base = requests.get(f"{API}/universe", timeout=60).json()
    assert "status" not in base and base["series"], base
    mism = requests.get(f"{API}/universe", params={"book": "TigerGlobal"}, timeout=60).json()
    assert mism["status"] == "book_mismatch", mism
    assert mism["requested_book"] == "TigerGlobal" and mism["artifact_book"] == "Soros", mism
    assert mism["kind"] == "membership", mism


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
