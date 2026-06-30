"""
test_views_api.py — tests for the /views REST wrapper (views_api.py), docs/vite-ui-plan.md §6.

No pytest, no cube: mount ONLY the views_api router on a bare FastAPI app (so there's no Atoti
lifespan to build) and drive it with FastAPI's TestClient against a TEMP VIEWS_ROOT. The pure
repository logic is already covered by test_views_repo.py; this asserts the HTTP wiring — status
codes, request/response shapes, and that every views_repo function is reachable end to end.

Run with the project venv:
    ../barra/bin/python test_views_api.py
"""
from __future__ import annotations
import tempfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import views_repo as R
from views_api import router

RESULTS = []


def test(fn):
    RESULTS.append(fn)
    return fn


def _client() -> TestClient:
    """A fresh temp VIEWS_ROOT + a bare app with just the views router (no cube)."""
    d = Path(tempfile.mkdtemp(prefix="viewapitest_"))
    R.VIEWS_ROOT = d
    R.ensure_root()
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


STATE = {"rows": ["Issuer"], "cols": [], "measures": ["Total VaR 99"],
         "slice_dims": ["Date"], "filters": {"Date": ["2024-12-31"]},
         "render": "grid"}


@test
def test_list_empty_has_both_sections():
    c = _client()
    r = c.get("/views")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["sections"]) == {"Public", "Private"}
    assert body["sections"]["Public"] == {"folders": {}, "views": []}


@test
def test_save_then_load_roundtrip():
    c = _client()
    r = c.put("/views/save", json={"name": "My View", "folder": "Public", "state": STATE})
    assert r.status_code == 200, r.text
    rel = r.json()["file"]
    assert rel == "Public/my-view.json"
    got = c.get(f"/views/item/{rel}")
    assert got.status_code == 200, got.text
    doc = got.json()
    assert doc["name"] == "My View"
    assert doc["state"]["measures"] == ["Total VaR 99"]
    assert doc["schema_version"] == R.SCHEMA_VERSION


@test
def test_save_shows_in_section_tree():
    c = _client()
    c.put("/views/save", json={"name": "Risk Top", "folder": "Public", "state": STATE})
    tree = c.get("/views", params={"section": "Public"}).json()["tree"]
    names = [v["name"] for v in tree["views"]]
    assert "Risk Top" in names


@test
def test_bad_section_400():
    c = _client()
    assert c.get("/views", params={"section": "Nope"}).status_code == 400


@test
def test_load_missing_404():
    c = _client()
    assert c.get("/views/item/Public/ghost.json").status_code == 404


@test
def test_folder_create_and_save_into_it():
    c = _client()
    fr = c.post("/views/folder", json={"parent": "Public", "name": "Desk Risk"})
    assert fr.status_code == 200, fr.text
    folder = fr.json()["folder"]
    assert folder == "Public/Desk Risk"
    sv = c.put("/views/save", json={"name": "v1", "folder": folder, "state": STATE})
    assert sv.json()["file"] == "Public/Desk Risk/v1.json"
    sub = c.get("/views", params={"section": "Public"}).json()["tree"]
    assert "Desk Risk" in sub["folders"]


@test
def test_move_view_between_folders():
    c = _client()
    c.post("/views/folder", json={"parent": "Public", "name": "A"})
    c.post("/views/folder", json={"parent": "Public", "name": "B"})
    c.put("/views/save", json={"name": "mv", "folder": "Public/A", "state": STATE})
    r = c.post("/views/move", json={"file": "Public/A/mv.json", "to_folder": "Public/B"})
    assert r.status_code == 200, r.text
    assert r.json()["file"] == "Public/B/mv.json"
    assert c.get("/views/item/Public/A/mv.json").status_code == 404
    assert c.get("/views/item/Public/B/mv.json").status_code == 200


@test
def test_rename_view():
    c = _client()
    c.put("/views/save", json={"name": "old name", "folder": "Public", "state": STATE})
    r = c.post("/views/rename", json={"file": "Public/old-name.json", "new_name": "new name"})
    assert r.status_code == 200, r.text
    assert r.json()["file"] == "Public/new-name.json"
    assert c.get("/views/item/Public/old-name.json").status_code == 404


@test
def test_delete_view_idempotent():
    c = _client()
    c.put("/views/save", json={"name": "tmp", "folder": "Public", "state": STATE})
    assert c.delete("/views/item/Public/tmp.json").status_code == 200
    assert c.get("/views/item/Public/tmp.json").status_code == 404
    # second delete is a no-op (idempotent), still 200
    assert c.delete("/views/item/Public/tmp.json").status_code == 200


@test
def test_delete_nonempty_folder_400():
    c = _client()
    c.post("/views/folder", json={"parent": "Public", "name": "Full"})
    c.put("/views/save", json={"name": "x", "folder": "Public/Full", "state": STATE})
    r = c.request("DELETE", "/views/folder/Public/Full")
    assert r.status_code == 400, r.text


@test
def test_rename_and_delete_empty_folder():
    c = _client()
    c.post("/views/folder", json={"parent": "Public", "name": "Temp"})
    rr = c.post("/views/folder/rename", json={"rel": "Public/Temp", "new_name": "Renamed"})
    assert rr.status_code == 200, rr.text
    assert rr.json()["folder"] == "Public/Renamed"
    dr = c.request("DELETE", "/views/folder/Public/Renamed")
    assert dr.status_code == 200, dr.text


@test
def test_traversal_rejected():
    c = _client()
    # a path escaping the root must not load (the _safe_rel guard -> 400, never a file outside)
    r = c.get("/views/item/../../etc/passwd")
    assert r.status_code in (400, 404), r.text


def main():
    passed = 0
    for fn in RESULTS:
        try:
            fn()
            print(f"PASS {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:                       # noqa: BLE001
            print(f"ERROR {fn.__name__}: {e.__class__.__name__}: {e}")
    print(f"\n{passed}/{len(RESULTS)} passed")
    raise SystemExit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
