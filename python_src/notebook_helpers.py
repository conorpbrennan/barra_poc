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
    session, cube = build_cube(load_frames(), port=port)
    # The notebook container is jailed to 2 CPUs, and on the 11-book frames the scenario-vector
    # queries (e.g. an Evt window's Scenario PnL) can exceed ActivePivot's 30s default query time
    # limit there — the host API cube on all cores never hits it. Raised for this session only.
    cube.shared_context["queriesTimeLimit"] = 180
    return session, cube


# Anchor colours of matplotlib's "Blues" (ColorBrewer), so the pure-python ramp below matches the
# app's look without importing matplotlib — the notebook container is air-gapped and ships only
# pure-python libs (altair/narwhals staged at data/_pylibs), so Styler.background_gradient's
# matplotlib dependency is exactly the thing we can't have.
_BLUES = [(247, 251, 255), (198, 219, 239), (107, 174, 214), (33, 113, 181), (8, 48, 107)]


def _blues_css(s: pd.Series) -> list[str]:
    """Per-column CSS for a Blues heatmap: min→lightest, max→darkest, NaN→unstyled. Text flips to
    white on dark cells (same intent as pandas' text_color_threshold)."""
    v = pd.to_numeric(s, errors="coerce")
    lo, hi = v.min(), v.max()
    if pd.isna(lo) or hi == lo:
        return [""] * len(s)
    out = []
    for x in (v - lo) / (hi - lo):
        if pd.isna(x):
            out.append("")
            continue
        seg = min(int(x * (len(_BLUES) - 1)), len(_BLUES) - 2)
        t = x * (len(_BLUES) - 1) - seg
        r, g, b = (round(a + (b_ - a) * t) for a, b_ in zip(_BLUES[seg], _BLUES[seg + 1]))
        lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255
        out.append(f"background-color: rgb({r},{g},{b}); color: {'#f1f1f1' if lum < 0.45 else '#111'}")
    return out


def style_grid(df: pd.DataFrame, *, pct: bool = True, prec: int = 3):
    """Return a pandas Styler reproducing the app's grid look functionally: a per-column blue
    heatmap over the numeric measure columns + percent/fixed formatting + an em-dash for nulls.

    `pct`/`prec` mirror a view's `as_pct`/`prec` state. Every Soros 13F grid is as_pct with
    prec=3, so the defaults match; pass pct=False for a plain fixed-decimal grid.
    """
    num = list(df.select_dtypes("number").columns)
    fmt = (f"{{:.{prec}%}}" if pct else f"{{:.{prec}f}}")
    return (df.style
              .apply(_blues_css, subset=num, axis=0)   # per-column heatmap, like the app
              .format({c: fmt for c in num}, na_rep="—"))
