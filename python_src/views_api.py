"""
views_api.py
============
Thin REST wrapper over views_repo.py — the ONE backend addition the new Vite UI (flexagg2++)
needs (docs/vite-ui-plan.md §6). The Streamlit app calls views_repo in-process; a separate SPA
can't, so these routes expose the same pure functions over HTTP. No new storage, no new logic:
every handler is a direct call into views_repo, which stays the single source of truth.

Mounted on the main FastAPI app in risk_api.py via `app.include_router(router)`. Because it only
touches views_repo (file-backed JSON under views/), it has NO cube dependency — so test_views_api.py
can exercise it on a bare FastAPI app with a temp VIEWS_ROOT, no Atoti session required.

Routes (all under /views; gated by the same nginx basic-auth as everything else):
    GET    /views?section=            -> list_tree (one section, or both when omitted)
    GET    /views/item/{rel:path}     -> load_view (rel = "Public/folder/slug.json")
    PUT    /views/save                -> save_view  (body {name, folder, state}) -> {file}
    DELETE /views/item/{rel:path}     -> delete_view
    POST   /views/move                -> move_view   (body {file, to_folder})    -> {file}
    POST   /views/rename              -> rename_view (body {file, new_name})      -> {file}
    POST   /views/folder              -> make_folder (body {parent, name})        -> {folder}
    POST   /views/folder/rename       -> rename_folder (body {rel, new_name})     -> {folder}
    DELETE /views/folder/{rel:path}   -> delete_folder
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

import views_repo as R

router = APIRouter(prefix="/views", tags=["views"])


# ----------------------------------------------------------------------------- request bodies
class SaveBody(BaseModel):
    name: str
    folder: str = ""              # rel folder under a section, e.g. "Public/Risk"
    state: dict                   # the STATE_FIELDS payload (rows/cols/measures/chart/...)


class MoveBody(BaseModel):
    file: str                     # rel file, e.g. "Public/old/slug.json"
    to_folder: str = ""           # destination rel folder ("" = a section root is required)


class RenameBody(BaseModel):
    file: str
    new_name: str


class FolderBody(BaseModel):
    parent: str = ""              # rel parent folder ("" = repository root; conventionally a section)
    name: str


class FolderRenameBody(BaseModel):
    rel: str
    new_name: str


# ----------------------------------------------------------------------------- list / load
@router.get("")
async def list_views(section: str | None = Query(None, description="Public | Private; both if omitted")):
    """The saved-view tree. With `section`, return that subtree; otherwise return each top-level
    section so the Repository panel can render both Public and Private at once."""
    R.ensure_root()
    if section:
        if section not in R.SECTIONS:
            raise HTTPException(400, f"section must be one of {R.SECTIONS}")
        return {"section": section, "tree": R.list_tree(section)}
    return {"sections": {s: R.list_tree(s) for s in R.SECTIONS}}


@router.get("/item/{rel:path}")
async def get_view(rel: str):
    """Load one saved view by its rel file path (section-prefixed, e.g. Public/x/slug.json)."""
    try:
        return R.load_view(rel)
    except FileNotFoundError:
        raise HTTPException(404, f"view not found: {rel}")
    except ValueError as e:                       # _safe_rel traversal guard
        raise HTTPException(400, str(e))


# ----------------------------------------------------------------------------- create / update
@router.put("/save")
async def put_view(body: SaveBody):
    """Create or overwrite a view. Identity is (folder, slug-of-name); re-saving the same name to
    the same folder overwrites and preserves `created`. Returns the rel file path it wrote."""
    try:
        rel = R.save_view(body.name, body.folder, body.state)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"file": rel}


@router.delete("/item/{rel:path}")
async def remove_view(rel: str):
    """Delete a saved view (idempotent — deleting a missing file is a no-op)."""
    try:
        R.delete_view(rel)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"deleted": rel}


@router.post("/move")
async def move(body: MoveBody):
    """Move a view to another folder (keeps its name/state, rewrites under the new folder)."""
    try:
        rel = R.move_view(body.file, body.to_folder)
    except FileNotFoundError:
        raise HTTPException(404, f"view not found: {body.file}")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"file": rel}


@router.post("/rename")
async def rename(body: RenameBody):
    """Rename a view in place (stays in its current folder)."""
    try:
        rel = R.rename_view(body.file, body.new_name)
    except FileNotFoundError:
        raise HTTPException(404, f"view not found: {body.file}")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"file": rel}


# ----------------------------------------------------------------------------- folders
@router.post("/folder")
async def add_folder(body: FolderBody):
    """Create a folder (nested directory) under `parent`."""
    try:
        rel = R.make_folder(body.parent, body.name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"folder": rel}


@router.post("/folder/rename")
async def rename_folder_route(body: FolderRenameBody):
    try:
        rel = R.rename_folder(body.rel, body.new_name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"folder": rel}


@router.delete("/folder/{rel:path}")
async def remove_folder(rel: str):
    """Delete an EMPTY folder (mirrors views_repo.delete_folder — non-empty raises 400)."""
    try:
        R.delete_folder(rel)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"deleted": rel}
