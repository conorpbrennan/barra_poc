"""
notebook_helpers.py — tiny shared utilities for the direct-Atoti demo notebook
(`notebooks/soros_13f_risk.ipynb`).

This is deliberately NOT a view interpreter. The notebook's whole point is to show the Atoti
Python API directly: every view is reconstructed as explicit, literal `cube.query(...)` calls in
its own cell. All this module does is (1) build the cube once and (2) keep the 8 grid cells short
by centralising the pandas-Styler formatting. The query logic stays in the notebook, visible.

Run the notebook (and anything importing this) with `PYTHONPATH=python_src` so the bare imports
below resolve — never prefix imports with `python_src`.
"""
from __future__ import annotations
import pandas as pd
from barra_factor_risk_cube import load_frames, build_cube

# A free port: the standalone Atoti UI owns :9090 and risk_api uses :9091/:9095, so the notebook's
# own session takes :9096. (We do not expose this web app — the notebook is the only surface.)
CUBE_PORT = 9096


def build(port: int = CUBE_PORT):
    """Build the factor-risk cube from the six parquet frames and return (session, cube).

    One call per kernel — the build is the slow step (~1–2 min on a warm tmp/ cache); every view
    cell then queries the same live cube instantly. Grab the query handles right after:

        session, cube = build()
        h, l, m = cube.hierarchies, cube.levels, cube.measures
    """
    return build_cube(load_frames(), port=port)


def style_grid(df: pd.DataFrame, *, pct: bool = True, prec: int = 3, cmap: str = "Blues"):
    """Return a pandas Styler reproducing the app's grid look functionally: a per-column blue
    heatmap over the numeric measure columns + percent/fixed formatting + an em-dash for nulls.

    `pct`/`prec` mirror a view's `as_pct`/`prec` state. Every Soros 13F grid is as_pct with
    prec=3, so the defaults match; pass pct=False for a plain fixed-decimal grid.
    """
    num = list(df.select_dtypes("number").columns)
    fmt = (f"{{:.{prec}%}}" if pct else f"{{:.{prec}f}}")
    return (df.style
              .background_gradient(cmap=cmap, subset=num, axis=0)   # per-column, like the app
              .format({c: fmt for c in num}, na_rep="—"))
