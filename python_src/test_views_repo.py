"""
test_views_repo.py — unit tests for the pure view-repository layer (views_repo.py).

No pytest dependency: plain asserts + a tiny runner. Run with the project venv:
    ../barra/bin/python test_views_repo.py
Exits non-zero if any test fails. Uses a TEMP VIEWS_ROOT, so it never touches the real repo.
"""
from __future__ import annotations
import json
import tempfile
import shutil
import time
from pathlib import Path

import views_repo as R

RESULTS = []


def test(fn):
    RESULTS.append(fn)
    return fn


def _fresh_root():
    """Point views_repo at a brand-new temp dir and return it."""
    d = Path(tempfile.mkdtemp(prefix="viewtest_"))
    R.VIEWS_ROOT = d
    R.ensure_root()
    return d


STATE = {"rows": ["Issuer"], "cols": [], "measures": ["Total VaR 99"],
         "slice_dims": ["Date"], "filters": {"Date": ["2024-12-31"]},
         "row_tot": False, "col_tot": False, "as_pct": True,
         "hide_empty": True, "heat": True, "prec": 3,
         "sort": [{"colId": "Net exposure", "sort": "desc", "sortIndex": 0}]}


@test
def t_ensure_root_makes_sections():
    d = _fresh_root()
    assert (d / "Public").is_dir() and (d / "Private").is_dir()


@test
def t_slugify():
    assert R.slugify("L2 — Factor Contributions") == "l2-factor-contributions"
    assert R.slugify("  ") == "view"
    assert R.slugify("a/b c") == "a-b-c"


@test
def t_folder_name_preserves_case_and_space():
    assert R.folder_name("My Risk Folder") == "My Risk Folder"
    assert R.folder_name("Soros 13F filings") == "Soros 13F filings"
    assert R.folder_name("  trimmed  ") == "trimmed"
    assert R.folder_name("a/b\\c") == "a b c"          # separators -> space
    assert R.folder_name("..") == "folder"
    assert R.folder_name("") == "folder"


@test
def t_safe_rel_rejects_traversal():
    _fresh_root()
    for bad in ["../evil", "a/../../b", "../../etc"]:
        try:
            R._safe_rel(bad)
            raise AssertionError(f"should have rejected {bad!r}")
        except ValueError:
            pass
    # absolute / backslash get neutralised but must stay INSIDE the root (no escape)
    root = R.VIEWS_ROOT.resolve()
    for s in ["/etc/passwd", "..\\win"]:
        full = R._safe_rel(s)
        assert root == full or root in full.parents, f"{s!r} escaped: {full}"


@test
def t_save_load_roundtrip():
    _fresh_root()
    rel = R.save_view("Book VaR Summary", "Public", STATE)
    assert rel == "Public/book-var-summary.json"
    doc = R.load_view(rel)
    assert doc["name"] == "Book VaR Summary"
    assert doc["path"] == "Public"
    assert doc["state"] == STATE
    assert doc["schema_version"] == R.SCHEMA_VERSION


@test
def t_save_root_no_folder():
    _fresh_root()
    rel = R.save_view("Top Level", "", STATE)
    assert rel == "top-level.json"
    assert (R.VIEWS_ROOT / "top-level.json").exists()


@test
def t_overwrite_preserves_created_bumps_updated():
    _fresh_root()
    rel = R.save_view("V", "Public", STATE)
    first = R.load_view(rel)
    time.sleep(1.05)                                   # _now_iso has 1s resolution
    R.save_view("V", "Public", {**STATE, "prec": 5})
    second = R.load_view(rel)
    assert second["created"] == first["created"], "created must be preserved"
    assert second["updated"] != first["updated"], "updated must bump"
    assert second["state"]["prec"] == 5
    assert not list(R.VIEWS_ROOT.rglob("*.tmp")), "atomic write left a .tmp file"


@test
def t_make_folder_nested_and_case():
    _fresh_root()
    rel = R.make_folder("Public", "Soros 13F filings")
    assert rel == "Public/Soros 13F filings"
    assert (R.VIEWS_ROOT / "Public" / "Soros 13F filings").is_dir()


@test
def t_all_folders_lists_nested():
    _fresh_root()
    R.make_folder("Public", "A")
    R.make_folder("Public/A", "B")
    fl = R.all_folders()
    assert "Public" in fl and "Public/A" in fl and "Public/A/B" in fl


@test
def t_list_tree_structure():
    _fresh_root()
    R.make_folder("Public", "Examples")
    R.save_view("L1", "Public/Examples", STATE)
    tree = R.list_tree()
    pub = tree["folders"]["Public"]
    ex = pub["folders"]["Examples"]
    names = [v["name"] for v in ex["views"]]
    assert names == ["L1"], names
    assert ex["views"][0]["file"] == "Public/Examples/l1.json"


@test
def t_rename_view_keeps_folder():
    _fresh_root()
    R.make_folder("Public", "F")
    rel = R.save_view("Old Name", "Public/F", STATE)
    new = R.rename_view(rel, "New Name")
    assert new == "Public/F/new-name.json"
    assert R.load_view(new)["name"] == "New Name"
    assert not R._safe_rel(rel).exists(), "old file should be gone"
    assert R.load_view(new)["state"] == STATE


@test
def t_move_view_across_folders():
    _fresh_root()
    R.make_folder("Public", "Src")
    rel = R.save_view("Mover", "Public/Src", STATE)
    new = R.move_view(rel, "Private")
    assert new == "Private/mover.json"
    assert (R.VIEWS_ROOT / "Private" / "mover.json").exists()
    assert not R._safe_rel(rel).exists(), "source should be removed after move"


@test
def t_delete_view():
    _fresh_root()
    rel = R.save_view("Doomed", "Public", STATE)
    R.delete_view(rel)
    assert not R._safe_rel(rel).exists()


@test
def t_rename_folder_with_spaces():
    _fresh_root()
    R.make_folder("Public", "old")
    new = R.rename_folder("Public/old", "New Spaced Name")
    assert new == "Public/New Spaced Name"
    assert (R.VIEWS_ROOT / "Public" / "New Spaced Name").is_dir()
    assert not (R.VIEWS_ROOT / "Public" / "old").exists()


@test
def t_delete_folder_only_when_empty():
    _fresh_root()
    R.make_folder("Public", "Keep")
    R.save_view("X", "Public/Keep", STATE)
    try:
        R.delete_folder("Public/Keep")
        raise AssertionError("should refuse to delete a non-empty folder")
    except ValueError:
        pass
    R.delete_view("Public/Keep/x.json")
    R.delete_folder("Public/Keep")
    assert not (R.VIEWS_ROOT / "Public" / "Keep").exists()


@test
def t_chart_fields_in_schema_and_roundtrip():
    _fresh_root()
    for f in ("render", "queries", "chart", "date_fmt"):
        assert f in R.STATE_FIELDS, f
    assert R.RENDERS == ["grid", "chart"]
    spec = {"source": "Q1", "mark": "line", "encoding": {"x": {"field": "Date", "type": "temporal"}}}
    queries = [{"name": "Q1", "rows": ["Date"], "cols": [], "measures": ["Net exposure"]}]
    rel = R.save_view("Chart", "Public", {**STATE, "render": "chart",
                                          "queries": queries, "chart": spec})
    st = R.load_view(rel)["state"]
    assert st["render"] == "chart"
    assert st["queries"] == queries
    assert st["chart"] == spec        # the full Vega-Lite spec round-trips verbatim


@test
def t_json_safe_records_nan_to_null():
    import math
    recs = [{"a": 1.0, "b": float("nan"), "c": "x"},
            {"a": None, "b": 2.5, "c": None},
            {"a": math.nan, "b": 0.0, "c": ""}]
    out = R.json_safe_records(recs)
    assert out == [{"a": 1.0, "b": None, "c": "x"},
                   {"a": None, "b": 2.5, "c": None},
                   {"a": None, "b": 0.0, "c": ""}], out
    s = json.dumps(out)                          # the bug: NaN literal is invalid JSON
    assert "NaN" not in s and "null" in s


@test
def t_json_safe_records_numpy_nan():
    import numpy as np
    out = R.json_safe_records([{"x": np.float64("nan"), "y": np.float64(3.5)}])
    assert out[0]["x"] is None and out[0]["y"] == 3.5
    assert "NaN" not in json.dumps([{k: float(v) if v is not None else None
                                     for k, v in out[0].items()}])


def main():
    roots = []
    passed = failed = 0
    for fn in RESULTS:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"FAIL  {fn.__name__}: {type(e).__name__}: {e}")
            failed += 1
        finally:
            r = getattr(R, "VIEWS_ROOT", None)
            if r and "viewtest_" in str(r):
                roots.append(r)
    for r in set(str(x) for x in roots):
        shutil.rmtree(r, ignore_errors=True)
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
