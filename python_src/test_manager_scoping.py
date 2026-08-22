"""Every manager-aware endpoint must actually honour `manager`.

The bug this exists to catch has no error and no stack trace: a cube query that omits the Manager
slice returns the no-manager grand total, which is NOT a portfolio — `single_value` refuses to
pick between two managers' differing weights for a shared name, so the aggregate collapses.
Measured on the 11-manager build, /risk read Scenario VaR 99 = 0.0013 unsliced against 0.0352 for
Soros: a 26x understatement served as an ordinary answer.

It went unnoticed for years because there was exactly one manager, so "sliced" and "unsliced" were
the same query. The whole existing suite passed while five endpoints ignored `manager` entirely.
So this asserts the INVARIANT rather than any particular number: two different managers must not
produce byte-identical responses. It is trivially satisfied on single-manager data (skips), and
becomes load-bearing the moment a second manager exists.
"""
import json
import os

API = os.environ.get("RISK_API", "http://127.0.0.1:8010")
_tests: list = []


def integ(fn):
    _tests.append(fn)
    return fn


def _backend_up():
    try:
        import requests
        return requests.get(f"{API}/dims", timeout=60).status_code == 200
    except Exception:
        return False


def _managers() -> list[str]:
    """Real managers, from /meta (never a hardcoded list)."""
    import requests
    j = requests.get(f"{API}/meta", timeout=60).json()
    return sorted(m["manager"] for m in (j.get("managers") or []))


# Endpoint -> query string template. Two managers, same params otherwise; responses must differ.
# `{d}` is the latest date. Kept to endpoints whose numbers are weight-dependent.
CASES = [
    ("/risk",          "date={d}&set=HistFull"),
    ("/scenarios",     "date={d}"),
    ("/exposures",     "date={d}"),
    ("/attribution",   "date={d}&set=HistFull&by=sector"),
    ("/validation",    ""),
    ("/scenario_pnl",  "date={d}&set=HistFull"),
    ("/contributions", "date={d}"),
    ("/hedge",         "date={d}"),
    ("/backtest",      "set=HistFull"),
    ("/drawdown",      "set=HistFull"),
    ("/trends",        "set=HistFull&measures=Scenario%20VaR%2099"),
    ("/limits",        ""),
]


@integ
def t_every_weight_dependent_endpoint_honours_manager():
    import requests
    managers = _managers()
    if len(managers) < 2:
        print(f"SKIP: single-manager data ({managers}) — invariant is vacuous")
        return
    a, b = managers[0], managers[-1]
    dates = requests.get(f"{API}/meta", timeout=60).json()["dates"]
    d = dates[-1]
    ignored = []
    for ep, qs in CASES:
        q = qs.format(d=d)
        sep = "&" if q else ""
        ra = requests.get(f"{API}{ep}?{q}{sep}manager={a}", timeout=180)
        rb = requests.get(f"{API}{ep}?{q}{sep}manager={b}", timeout=180)
        assert ra.status_code == 200, (ep, a, ra.status_code, ra.text[:200])
        assert rb.status_code == 200, (ep, b, rb.status_code, rb.text[:200])
        if ra.text == rb.text:
            ignored.append(ep)
    assert not ignored, (
        f"these endpoints returned IDENTICAL responses for manager={a} and manager={b}, i.e. they "
        f"ignore the manager and are serving the collapsed no-manager grand total: {ignored}")


@integ
def t_unsliced_and_sliced_differ_where_it_matters():
    """/risk with no manager param must not silently equal any single manager's answer — the
    default exists so the endpoint is never accidentally unscoped, and the default IS a real
    manager."""
    import requests
    managers = _managers()
    if len(managers) < 2:
        print("SKIP: single-manager data")
        return
    d = requests.get(f"{API}/meta", timeout=60).json()["dates"][-1]
    default = requests.get(f"{API}/risk?date={d}&set=HistFull", timeout=60).json()
    assert default.get("factor_var"), default
    # the default must match exactly one named manager (its own), not the collapsed aggregate
    matches = []
    for mgr in managers:
        j = requests.get(f"{API}/risk?date={d}&set=HistFull&manager={mgr}", timeout=60).json()
        if j.get("factor_var") == default.get("factor_var"):
            matches.append(mgr)
    assert matches, ("the /risk default matches NO manager — it is serving a collapsed aggregate",
                     default.get("factor_var"))


if __name__ == "__main__":
    print("=== manager-scoping invariant (integ needs :8010) ===")
    if not _backend_up():
        print(f"SKIP: backend not reachable at {API}")
        raise SystemExit(0)
    p = f = 0
    for fn in _tests:
        try:
            fn(); print(f"PASS  {fn.__name__}"); p += 1
        except AssertionError as e:
            print(f"FAIL  {fn.__name__}: {e}"); f += 1
    print(f"\n{p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
