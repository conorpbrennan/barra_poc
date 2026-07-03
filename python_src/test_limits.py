"""
test_limits.py — checks for the desk-limit RAG monitoring (Step 1).

  * UNIT  — always run, no backend: _rag traffic-light logic, and limits.json parses with the
            expected shape.
  * INTEG — need the live backend on :8010; SKIP if down. /limits returns a configured RAG status
            with well-formed checks, and the book Total VaR 99 limit is evaluated.

Run:  BARRA_API=http://127.0.0.1:8010 ../barra/bin/python test_limits.py
"""
from __future__ import annotations
import os
import json
import pathlib

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
def t_rag_thresholds():
    """_rag: green below warn, amber in [warn, limit), breach at/above limit; headroom signed."""
    import risk_api
    assert risk_api._rag(0.03, 0.045, 0.055)[0] == "green"
    assert risk_api._rag(0.05, 0.045, 0.055)[0] == "amber"
    assert risk_api._rag(0.06, 0.045, 0.055)[0] == "breach"
    assert risk_api._rag(None, 0.045, 0.055)[0] == "unknown"
    _, head = risk_api._rag(0.06, 0.045, 0.055)
    assert abs(head - (0.055 - 0.06)) < 1e-12, head      # negative once breached


@unit
def t_rag_no_warn_is_optional():
    """A limit with no 'warn' goes straight green -> breach (no amber band)."""
    import risk_api
    assert risk_api._rag(0.05, None, 0.10)[0] == "green"
    assert risk_api._rag(0.10, None, 0.10)[0] == "breach"


@unit
def t_limits_json_shape():
    """The shipped limits.json parses and has book + concentration limits with numeric caps."""
    import risk_api
    cfg = risk_api._load_limits()
    assert cfg, "limits.json missing or empty"
    assert "Total VaR 99" in cfg.get("book", {}), cfg.get("book")
    for spec in cfg["book"].values():
        assert isinstance(spec.get("limit"), (int, float)), spec
    assert "single_name_weight" in cfg.get("concentration", {}), cfg.get("concentration")


# --------------------------------------------------------------------------- INTEG
@integ
def t_limits_endpoint_configured():
    """/limits returns a configured RAG status with well-formed checks."""
    import requests
    d = requests.get(f"{API}/limits", timeout=30); d.raise_for_status()
    j = d.json()
    assert j["configured"] is True, j
    assert j["status"] in ("green", "amber", "breach", "unknown", "none"), j["status"]
    assert j["checks"], "no checks evaluated"
    for c in j["checks"]:
        for k in ("name", "scope", "value", "limit", "status", "headroom"):
            assert k in c, (k, c)
        assert c["status"] in ("green", "amber", "breach", "unknown"), c


@integ
def t_limits_evaluates_book_var():
    """The Total VaR 99 limit is present and (when data exists) carries a numeric value + headroom."""
    import requests
    j = requests.get(f"{API}/limits", timeout=30).json()
    var = next((c for c in j["checks"] if c["name"] == "Total VaR 99"), None)
    assert var is not None, "Total VaR 99 limit not evaluated"
    if var["value"] is not None:                          # green/amber/breach (not unknown)
        assert var["status"] != "unknown"
        assert abs(var["headroom"] - (var["limit"] - var["value"])) < 1e-9, var


@integ
def t_limits_set_override():
    """`set` overrides the scenario set the book VaR/ES limits read against; the Top-5 risk
    share is set-INDEPENDENT by design (what-if math on the full history) so it must not move."""
    import requests
    base = requests.get(f"{API}/limits", timeout=30).json()
    hypo = requests.get(f"{API}/limits", params={"set": "Hypo:MomentumCrash"}, timeout=30).json()
    assert hypo["set"] == "Hypo:MomentumCrash", hypo["set"]
    bv = next((c for c in base["checks"] if c["name"] == "Total VaR 99"), None)
    hv = next((c for c in hypo["checks"] if c["name"] == "Total VaR 99"), None)
    if bv and hv and bv["value"] and hv["value"]:
        assert abs(bv["value"] - hv["value"]) > 1e-9                    # set changed the VaR read
    bt = next((c for c in base["checks"] if c["name"] == "Top-5 risk share"), None)
    ht = next((c for c in hypo["checks"] if c["name"] == "Top-5 risk share"), None)
    assert bt is not None and ht is not None
    if bt["value"] is not None:
        assert 0 < bt["value"] <= 1
        assert bt["value"] == ht["value"]                               # set-independent


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
