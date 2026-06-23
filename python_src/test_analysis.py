"""
test_analysis.py — checks for the /analysis risk-commentary endpoint and the /pivot refactor it
shares code with (risk_api.py).

Three tiers:
  * UNIT  — always run, no backend, NO tokens: the shared _validate_pivot guard rejects off-
            allowlist dims/measures, and _anthropic_key() finds the key.
  * INTEG — need the live backend on :8010; SKIP if down. The /pivot refactor still returns the
            same shape, and /analysis rejects a bad measure BEFORE any LLM call (so no spend).
  * LIVE  — opt-in only (RUN_LLM=1): actually stream /analysis and check it returns markdown.
            Costs a few cents; off by default so the suite never spends tokens.

The point of the INTEG bad-measure test: _validate_pivot runs ahead of the model, so a rejected
request never reaches Anthropic — the analysis endpoint can't be used to query off-allowlist
fields, and probing it is free.

Run:  BARRA_API=http://127.0.0.1:8010 ../barra/bin/python test_analysis.py
      RUN_LLM=1 BARRA_API=... ../barra/bin/python test_analysis.py   # include the live call
"""
from __future__ import annotations
import os
import json
import urllib.parse

API = os.environ.get("BARRA_API", "http://127.0.0.1:8010")
UNIT, INTEG, LIVE = [], [], []
DATE = None   # latest cube date, filled by _backend_up


def unit(fn):
    UNIT.append(fn); return fn


def integ(fn):
    INTEG.append(fn); return fn


def live(fn):
    LIVE.append(fn); return fn


def _backend_up():
    global DATE
    try:
        import requests
        d = requests.get(f"{API}/dims", timeout=5)
        if d.status_code != 200:
            return False
        DATE = d.json()["dates"][-1]
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- UNIT (no backend)
@unit
def t_validate_rejects_bad_measure():
    """_validate_pivot raises 400 for an off-allowlist measure — the same guard /analysis uses."""
    import risk_api
    from fastapi import HTTPException
    try:
        risk_api._validate_pivot(["ScenarioSet"], [], ["Not A Real Measure"], {})
    except HTTPException as e:
        assert e.status_code == 400, e.status_code
        return
    raise AssertionError("expected HTTPException(400) for a bad measure")


@unit
def t_validate_rejects_bad_dimension():
    """_validate_pivot raises 400 for an off-allowlist dimension."""
    import risk_api
    from fastapi import HTTPException
    try:
        risk_api._validate_pivot(["NotADim"], [], ["Net exposure"], {})
    except HTTPException as e:
        assert e.status_code == 400, e.status_code
        return
    raise AssertionError("expected HTTPException(400) for a bad dimension")


@unit
def t_validate_requires_rows_and_measures():
    """Empty measures and empty rows each raise 400 (matches /pivot's long-standing contract)."""
    import risk_api
    from fastapi import HTTPException
    for rows, meas in (([], ["Net exposure"]), (["ScenarioSet"], [])):
        try:
            risk_api._validate_pivot(rows, [], meas, {})
        except HTTPException as e:
            assert e.status_code == 400, e.status_code
            continue
        raise AssertionError(f"expected 400 for rows={rows} measures={meas}")


@unit
def t_validate_accepts_good_spec():
    """A valid spec passes the guard cleanly (no exception)."""
    import risk_api
    risk_api._validate_pivot(["ScenarioSet"], [], ["Scenario VaR 99"], {"Book": ["Soros"]})


@unit
def t_anthropic_key_present():
    """_anthropic_key() finds a key (env var or repo .env). Skips (does not fail) if neither has
    one, so a checkout without a key still passes the suite."""
    import risk_api
    key = risk_api._anthropic_key()
    if not key:
        print("    note: no ANTHROPIC_API_KEY in env or .env — analysis will 502 until one is set")
        return
    assert key.startswith("sk-"), "key present but not in sk-... form"


# --------------------------------------------------------------------------- INTEG (live backend)
@integ
def t_pivot_refactor_regression():
    """After extracting _pivot_result, /pivot still returns the same shape: the requested measure
    present in non-empty records, plus the rows/measures/warning envelope."""
    import requests
    filters = {"Book": ["Soros"], "Date": [DATE], "ScenarioSet": ["HistFull"]}
    q = {"rows": "ScenarioSet", "measures": "Scenario VaR 99",
         "filters": json.dumps(filters), "totals": "true"}
    r = requests.get(f"{API}/pivot?{urllib.parse.urlencode(q)}", timeout=60)
    r.raise_for_status()
    d = r.json()
    for k in ("rows", "cols", "measures", "warning", "records"):
        assert k in d, f"missing key {k}"
    assert d["records"], "no records"
    assert "Scenario VaR 99" in d["records"][0], d["records"][0]
    assert "grand" in d, "totals=true should add a grand corner"


@integ
def t_analysis_rejects_bad_measure_no_llm():
    """POST /analysis with an off-allowlist measure -> 400, BEFORE any model call (free to probe)."""
    import requests
    body = {"rows": "ScenarioSet", "measures": "Not A Real Measure",
            "filters": json.dumps({"Book": ["Soros"], "Date": [DATE]})}
    r = requests.post(f"{API}/analysis", json=body, timeout=30)
    assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text[:200]}"


@integ
def t_analysis_rejects_bad_dim_no_llm():
    """POST /analysis with an off-allowlist dimension -> 400 (the guard is shared with /pivot)."""
    import requests
    body = {"rows": "NotADim", "measures": "Net exposure",
            "filters": json.dumps({"Book": ["Soros"], "Date": [DATE]})}
    r = requests.post(f"{API}/analysis", json=body, timeout=30)
    assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text[:200]}"


# --------------------------------------------------------------------------- LIVE (RUN_LLM=1)
@live
def t_analysis_streams_markdown():
    """End-to-end: /analysis streams non-empty markdown for a real view. Costs a few cents."""
    import requests
    body = {"rows": "ScenarioSet", "measures": "Risk HHI",
            "filters": json.dumps({"Book": ["Soros"], "Date": [DATE]}),
            "name": "Concentration — Risk HHI (test)"}
    r = requests.post(f"{API}/analysis", json=body, stream=True, timeout=180)
    r.raise_for_status()
    r.encoding = "utf-8"
    text = "".join(c for c in r.iter_content(chunk_size=None, decode_unicode=True) if c)
    assert len(text.strip()) > 50, f"analysis too short: {text!r}"
    assert "analysis failed" not in text.lower(), text


def _run(group, label):
    passed = failed = 0
    for fn in group:
        try:
            fn(); print(f"PASS  {fn.__name__}"); passed += 1
        except Exception as e:
            import traceback
            print(f"FAIL  {fn.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc(); failed += 1
    return passed, failed


def main():
    p = f = 0
    print("=== unit (no backend) ===")
    a, b = _run(UNIT, "unit"); p += a; f += b

    print("\n=== integration (live backend) ===")
    if _backend_up():
        a, b = _run(INTEG, "integ"); p += a; f += b
        if os.environ.get("RUN_LLM") == "1":
            print("\n=== live LLM (RUN_LLM=1; spends tokens) ===")
            a, b = _run(LIVE, "live"); p += a; f += b
        else:
            print("\nSKIP live LLM tests (set RUN_LLM=1 to run the real /analysis call)")
    else:
        print(f"SKIP: backend not reachable at {API} (start risk_api on :8010 for integration tests)")

    print(f"\n{p} passed, {f} failed")
    raise SystemExit(1 if f else 0)


if __name__ == "__main__":
    main()
