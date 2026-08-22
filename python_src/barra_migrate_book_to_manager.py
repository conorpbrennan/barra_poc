"""One-off migration: rename the `Book` column to `Manager` in place on the two parquet frames
that carry it (`positions`, `managers`) — the physical cube-level rename (2026-08-22, see
CLAUDE.md "Buyside-list expansion" / the Manager-rename note).

Verified before writing this script: no other parquet under data/ carries a `Book` column
(universe_*, pnl_attribution.* are all keyed differently). Run once, from python_src/:

    ../barra/bin/python barra_migrate_book_to_manager.py

Idempotent: if a frame's `Book` column is already gone and `Manager` is already present, it is
left untouched (so a re-run after the rename is a clean no-op, not an error).

The arrow cache under data/.cube_cache/ keys itself on the source parquet's mtime + size, so it
re-keys itself automatically the next time the cube loads (see barra_factor_risk_cube.py) — this
script does not touch data/.cube_cache/.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"

FRAMES = ["positions.parquet", "managers.parquet"]


def _migrate_one(path: Path) -> None:
    if not path.exists():
        print(f"  SKIP {path.name}: file not found")
        return

    before = pd.read_parquet(path)
    before_dtypes = before.dtypes.to_dict()
    before_rows = len(before)

    if "Manager" in before.columns and "Book" not in before.columns:
        print(f"  SKIP {path.name}: already migrated (has Manager, no Book)")
        return

    assert "Book" in before.columns, f"{path.name}: expected a Book column, found {list(before.columns)}"
    assert "Manager" not in before.columns, f"{path.name}: Manager column already present alongside Book — ambiguous"

    print(f"  {path.name} BEFORE: rows={before_rows} cols={list(before.columns)}")
    for c, dt in before_dtypes.items():
        print(f"      {c}: {dt}")

    after = before.rename(columns={"Book": "Manager"})
    after.to_parquet(path, index=False)

    reread = pd.read_parquet(path)
    assert len(reread) == before_rows, f"{path.name}: row count changed {before_rows} -> {len(reread)}"
    assert "Manager" in reread.columns and "Book" not in reread.columns
    after_dtypes = reread.dtypes.to_dict()
    for c, dt in before_dtypes.items():
        newc = "Manager" if c == "Book" else c
        assert after_dtypes[newc] == dt, f"{path.name}: dtype changed for {c} -> {newc}: {dt} -> {after_dtypes[newc]}"

    print(f"  {path.name} AFTER:  rows={len(reread)} cols={list(reread.columns)}")
    for c, dt in after_dtypes.items():
        print(f"      {c}: {dt}")


def run() -> None:
    print(f"Migrating Book -> Manager in {DATA}")
    for name in FRAMES:
        print(f"\n{name}:")
        _migrate_one(DATA / name)
    print("\nDone.")


if __name__ == "__main__":
    run()
    sys.exit(0)
