"""
test_ask.py — checks for the scoped Q&A drill-down (Step 10; risk_api.py /ask + query_cube tool).

  UNIT  — always run, no backend, no tokens: the query_cube tool schema, and that _run_query_cube
          enforces the SAME allowlist as /pivot — off-allowlist dim/measure (or an empty rows/measures
          selection) comes back as an {"error": ...} dict (so the model can retry), NOT a raise, and
          WITHOUT touching the cube.
  INTEG — need the live backend on :8010; SKIP if down: an empty question is a clean 400 (raised before
          any token spend).
  LIVE  — opt-in only (RUN_LLM=1): actually stream /ask and check it returns markdown AND that the model
          pulled at least one slice (a `query_cube` marker appears in the stream).

Run:  BARRA_API=http://127.0.0.1:8010 ../barra/bin/python test_ask.py
      RUN_LLM=1 BARRA_API=... ../barra/bin/python test_ask.py   # include the live call
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
def t_tool_schema():
    import risk_api
    t = risk_api.QUERY_CUBE_TOOL
    assert t["name"] == "query_cube"
    props = t["input_schema"]["properties"]
    assert {"rows", "cols", "measures", "filters", "totals"} <= set(props)
    assert t["input_schema"]["required"] == ["rows", "measures"]
    # the description must enumerate the live allowlist so the model picks valid names
    assert "Net exposure" in t["description"] and "ScenarioSet" in t["description"]


@unit
def t_run_query_cube_rejects_off_allowlist():
    """The guard runs BEFORE the cube — bad names come back as an error dict, no cube needed."""
    import risk_api
    bad_dim = risk_api._run_query_cube({"rows": ["Bogus"], "measures": ["Net exposure"]})
    assert "error" in bad_dim and "dimension" in bad_dim["error"], bad_dim
    bad_meas = risk_api._run_query_cube({"rows": ["Factor"], "measures": ["Made up"]})
    assert "error" in bad_meas and "measure" in bad_meas["error"], bad_meas


@unit
def t_run_query_cube_requires_rows_and_measures():
    import risk_api
    no_meas = risk_api._run_query_cube({"rows": ["Factor"], "measures": []})
    assert "error" in no_meas and "measure" in no_meas["error"], no_meas
    no_rows = risk_api._run_query_cube({"rows": [], "measures": ["Net exposure"]})
    assert "error" in no_rows and "row" in no_rows["error"], no_rows


# ----------------------------------------------------------------------------- INTEG
@integ
def t_empty_question_is_400():
    import requests
    r = requests.post(f"{API}/ask", json={"question": "   "}, timeout=30)
    assert r.status_code == 400, (r.status_code, r.text)


# ----------------------------------------------------------------------------- LIVE (opt-in)
@live
def t_ask_streams_markdown_and_queries():
    import requests
    q = "Which single factor carries the largest net exposure at the latest date? Give the number."
    r = requests.post(f"{API}/ask", json={"question": q}, stream=True, timeout=300)
    r.raise_for_status(); r.encoding = "utf-8"
    text = "".join(c for c in r.iter_content(chunk_size=None, decode_unicode=True) if c)
    assert len(text) > 80, f"expected a markdown answer, got {len(text)} chars"
    assert "query_cube" in text, "expected the model to pull at least one slice (no query_cube marker)"


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
            print("\nSKIP live LLM (set RUN_LLM=1 to stream /ask)")
    else:
        print(f"SKIP: backend not reachable at {API}")
    print(f"\n{p} passed, {f} failed")
    raise SystemExit(1 if f else 0)


if __name__ == "__main__":
    main()
