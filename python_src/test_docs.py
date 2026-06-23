"""
test_docs.py — checks for the in-UI documentation (dashboard guide + model reference).

  * UNIT  — always run, no backend: the static dashboard guide exists, and the CRO-report generator
            (barra_cro_report.build) runs against the live frames and publishes the model reference
            to the served static dir with the new feature sections. The build() run also guards the
            barra_dq_checks.run() refactor (it consumes the structured results).
  * INTEG — needs the Streamlit UI on :8502 (BARRA_UI); SKIP if down. Both docs are served under
            app/static/ so the in-UI links resolve for remote users.

Run:  ../barra/bin/python test_docs.py
      BARRA_UI=http://127.0.0.1:8502 ../barra/bin/python test_docs.py   # include the served check
"""
from __future__ import annotations
import os
import pathlib

UI = os.environ.get("BARRA_UI", "http://127.0.0.1:8502")
BASE = "/flexagg++"
STATIC = pathlib.Path(__file__).resolve().parent / "static"
UNIT, INTEG = [], []


def unit(fn):
    UNIT.append(fn); return fn


def integ(fn):
    INTEG.append(fn); return fn


# --------------------------------------------------------------------------- UNIT
@unit
def t_guide_present_and_covers_features():
    """static/guide.html exists and documents the new panels."""
    p = STATIC / "guide.html"
    assert p.exists(), f"missing {p}"
    html = p.read_text()
    for token in ("Desk limits", "Data quality", "VaR backtest", "Risk trends", "commentary"):
        assert token in html, f"guide.html missing '{token}'"


@unit
def t_report_builds_and_publishes():
    """barra_cro_report.build() runs against the live frames and writes the model reference to BOTH
    tmp/ and the served static dir, including the new §7/§8 sections. Also guards that the report
    still consumes barra_dq_checks.run() after its refactor (a broken consume raises here)."""
    import barra_cro_report
    barra_cro_report.build()
    ref = STATIC / "barra_model_reference.html"
    assert ref.exists(), f"missing {ref}"
    html = ref.read_text()
    assert len(html) > 50_000, f"reference suspiciously small ({len(html)} bytes)"
    for token in ("Data &amp; Transformation Reference",
                  "VaR backtest &amp; model validation",   # §7 (new)
                  "Risk tooling on the cube",               # §8 (new)
                  "FHS (default", "Risk HHI", "Expected Shortfall"):
        assert token in html, f"model reference missing '{token}'"


@unit
def t_report_also_writes_tmp():
    """The legacy tmp/ artifact is still produced (the CLI workflow)."""
    tmp = pathlib.Path(__file__).resolve().parent.parent / "tmp" / "barra_model_reference.html"
    assert tmp.exists(), f"missing {tmp}"


# --------------------------------------------------------------------------- INTEG
@integ
def t_docs_served_by_ui():
    """Both docs are served under app/static/ so the in-UI links work for remote users."""
    import requests
    for name in ("guide.html", "barra_model_reference.html"):
        url = f"{UI}{BASE}/app/static/{name}"
        r = requests.get(url, timeout=15)
        assert r.status_code == 200, f"{url} -> {r.status_code}"
        assert "<html" in r.text.lower(), f"{url} did not return HTML"


def _backend_up(url):
    try:
        import requests
        return requests.get(f"{url}{BASE}/app/static/guide.html", timeout=5).status_code == 200
    except Exception:
        return False


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
    print("\n=== integration (Streamlit UI on :8502) ===")
    if _backend_up(UI):
        a, b = _run(INTEG); p += a; f += b
    else:
        print(f"SKIP: UI not reachable at {UI} (start the dashboard to run served-docs checks)")
    print(f"\n{p} passed, {f} failed")
    raise SystemExit(1 if f else 0)


if __name__ == "__main__":
    main()
