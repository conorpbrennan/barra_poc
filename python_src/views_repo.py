"""
views_repo.py
=============
Pure file/JSON repository for saved pivot "views" used by risk_pivot_app.py ("Flex Agg ++").

No Streamlit / pandas dependency — just the on-disk model, so it can be unit-tested directly
(see test_views_repo.py). Views are one flat JSON file per saved configuration under VIEWS_ROOT;
folders are real nested directories on disk (the source of truth). Every user-supplied path is
validated through _safe_rel to block traversal, and writes are atomic (tmp + os.replace).
"""
from __future__ import annotations
import os
import re
import json
import datetime as _dt
from pathlib import Path

VIEWS_ROOT = Path(__file__).resolve().parent.parent / "views"
SCHEMA_VERSION = 1
# the state fields that define a view (order is the canonical capture order)
STATE_FIELDS = ["rows", "cols", "measures", "slice_dims", "filters",
                "row_tot", "col_tot", "as_pct", "hide_empty", "heat", "prec", "sort",
                "date_fmt", "render", "queries", "chart"]
RENDERS = ["grid", "chart"]   # how a view draws: ag-grid table | an embedded Vega-Lite `chart` spec
# A "chart" view is fully self-describing: `queries` is a list of named, self-contained PIVOT
# queries (each `{name, rows, cols, measures, filters}` — the same tabular query the L1 grid runs)
# and `chart` is a COMPLETE Vega-Lite spec (marks/encodings/scales/theme). Each spec carries
# `"source": <query name>` linking the graph to ONE query (the graph's "source" IS a pivot query).
# The app renders generically — no chart logic in code; every query is served by /pivot and its
# records bound as the spec's default dataset. (Legacy `source` feeds are migrated on load.)
SECTIONS = ["Public", "Private"]   # top-level repository split (mirrors ActivePivot)


def ensure_root() -> Path:
    VIEWS_ROOT.mkdir(parents=True, exist_ok=True)
    for s in SECTIONS:                       # the two top-level sections always exist
        (VIEWS_ROOT / s).mkdir(exist_ok=True)
    return VIEWS_ROOT


def slugify(name: str) -> str:
    s = re.sub(r"[^\w\-]+", "-", (name or "").strip().lower()).strip("-")
    return s or "view"


def folder_name(name: str) -> str:
    """A folder is a real directory whose name IS its display label, so keep the user's case
    and spaces — only drop path separators / control chars and reject . / .. for safety."""
    seg = re.sub(r"[/\\\x00-\x1f]+", " ", (name or "")).strip()
    return seg if seg and seg not in (".", "..") else "folder"


def _safe_rel(rel: str) -> Path:
    """Resolve a user-supplied relative path under VIEWS_ROOT, rejecting absolute paths
    and any traversal that would escape the root. Returns the absolute resolved Path."""
    rel = (rel or "").strip().strip("/").strip("\\")
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"unsafe path: {rel!r}")
    root = VIEWS_ROOT.resolve()
    full = (root / p).resolve()
    if full != root and root not in full.parents:
        raise ValueError(f"path escapes views root: {rel!r}")
    return full


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def list_tree(rel: str = "") -> dict:
    """Walk VIEWS_ROOT from `rel` and return a nested
    {folders:{name: <subtree>}, views:[{name,slug,path,file,created,updated}]} structure."""
    ensure_root()
    base = _safe_rel(rel)
    folders: dict = {}
    views: list = []
    if base.exists():
        for entry in sorted(base.iterdir(), key=lambda e: e.name.lower()):
            child_rel = f"{rel}/{entry.name}".strip("/") if rel else entry.name
            if entry.is_dir():
                folders[entry.name] = list_tree(child_rel)
            elif entry.suffix == ".json":
                try:
                    doc = json.loads(entry.read_text())
                except Exception:
                    continue
                views.append({"name": doc.get("name", entry.stem),
                              "slug": entry.stem, "path": rel, "file": child_rel,
                              "created": doc.get("created"), "updated": doc.get("updated")})
    return {"folders": folders, "views": views}


def all_folders(rel: str = "") -> list[str]:
    """Flat sorted list of every folder (rel dir path) under VIEWS_ROOT, depth-first."""
    ensure_root()
    out: list[str] = []
    base = _safe_rel(rel)
    if base.exists():
        for entry in sorted(base.iterdir(), key=lambda e: e.name.lower()):
            if entry.is_dir():
                child_rel = f"{rel}/{entry.name}".strip("/") if rel else entry.name
                out.append(child_rel)
                out.extend(all_folders(child_rel))
    return out


def load_view(rel_file: str) -> dict:
    full = _safe_rel(rel_file)
    return json.loads(full.read_text())


def _atomic_write(full: Path, doc: dict) -> None:
    full.parent.mkdir(parents=True, exist_ok=True)
    tmp = full.with_suffix(full.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2))
    os.replace(tmp, full)


def save_view(name: str, folder: str, state: dict) -> str:
    """Write a view; identity is (folder, slug). Re-saving the same name to the same
    folder overwrites, preserving `created` and bumping `updated`. Returns rel file path."""
    ensure_root()
    slug = slugify(name)
    folder = (folder or "").strip().strip("/")
    rel_file = f"{folder}/{slug}.json".strip("/") if folder else f"{slug}.json"
    full = _safe_rel(rel_file)
    created = _now_iso()
    if full.exists():
        try:
            created = json.loads(full.read_text()).get("created", created)
        except Exception:
            pass
    doc = {"schema_version": SCHEMA_VERSION, "name": name, "path": folder,
           "created": created, "updated": _now_iso(), "state": state}
    _atomic_write(full, doc)
    return rel_file


def make_folder(parent: str, name: str) -> str:
    parent = (parent or "").strip().strip("/")
    leaf = folder_name(name)
    rel = f"{parent}/{leaf}".strip("/") if parent else leaf
    full = _safe_rel(rel)
    full.mkdir(parents=True, exist_ok=True)
    return rel


def rename_folder(rel: str, new_name: str) -> str:
    full = _safe_rel(rel)
    if not full.is_dir():
        raise ValueError(f"not a folder: {rel!r}")
    parent_rel = str(Path(rel).parent)
    parent_rel = "" if parent_rel == "." else parent_rel
    leaf = folder_name(new_name)
    new_rel = f"{parent_rel}/{leaf}".strip("/") if parent_rel else leaf
    new_full = _safe_rel(new_rel)
    os.replace(full, new_full)
    return new_rel


def delete_folder(rel: str) -> None:
    """Delete a folder; only if empty."""
    full = _safe_rel(rel)
    if not full.is_dir():
        raise ValueError(f"not a folder: {rel!r}")
    if full == VIEWS_ROOT.resolve():
        raise ValueError("cannot delete views root")
    if any(full.iterdir()):
        raise ValueError("folder not empty")
    full.rmdir()


def rename_view(rel_file: str, new_name: str) -> str:
    """Rename a view in place (keeps it in its CURRENT folder, derived from the file's real
    location — not the possibly-stale `path` stored in the JSON)."""
    full = _safe_rel(rel_file)
    doc = json.loads(full.read_text())
    folder = str(Path(rel_file).parent)
    folder = "" if folder == "." else folder
    new_rel = save_view(new_name, folder, doc.get("state", {}))
    new_full = _safe_rel(new_rel)
    if new_full != full:
        full.unlink(missing_ok=True)
    return new_rel


def move_view(rel_file: str, new_folder: str) -> str:
    full = _safe_rel(rel_file)
    doc = json.loads(full.read_text())
    new_rel = save_view(doc.get("name", full.stem), new_folder, doc.get("state", {}))
    new_full = _safe_rel(new_rel)
    if new_full != full:
        full.unlink(missing_ok=True)
    return new_rel


def delete_view(rel_file: str) -> None:
    _safe_rel(rel_file).unlink(missing_ok=True)


def json_safe_records(records: list) -> list:
    """Replace NaN / None with None in a list of {column: value} rows so json.dumps emits the
    valid `null` — NOT the literal `NaN`, which breaks ag-grid's frontend JSON.parse. (Floats
    can't hold None, so DataFrame.where(..., None) silently turns None back into NaN — hence we
    fix the records, not the frame.) NaN is detected by the `v != v` identity."""
    def _fix(v):
        if v is None:
            return None
        if isinstance(v, float) and v != v:        # NaN (incl. numpy float64 nan)
            return None
        return v
    return [{k: _fix(v) for k, v in r.items()} for r in records]
