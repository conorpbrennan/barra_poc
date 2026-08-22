"""
barra_build_stock_returns.py
=============================
Standalone precompute for the ninth (optional) frame: `stock_returns` (Date, Position) -> Return,
daily SIMPLE returns for every coverage name, historical-simulation input for the Price VaR family
(docs/price-var-plan.md, step 1).

Reuses `data/securities.parquet` (the already-resolved Position<->Ticker map -- no SEC/OpenFIGI
re-resolution) and the builder's cached price loaders (`stooq_daily` / `_yahoo_daily` / `_get` in
barra_build_frames.py, all disk-cached under repo-local `tmp/`), so on a warm cache this touches no
network at all -- it re-reads exactly the CSV/JSON responses the original build already pulled.
On a cold cache it re-pulls one Stooq/Yahoo series per name (the same cost the original build paid).

This is the SAME work `barra_build_frames.build_frames()` now also does inline (via
`stock_returns_from_prices`, sharing that in-memory `prices` dict for free) so a future full
rebuild emits this frame automatically. This script exists so the frame can be produced WITHOUT a
60-minute full rebuild against data already on disk.

Run (needs data/securities.parquet already built):
    cd python_src
    ../barra/bin/python barra_build_stock_returns.py

Writes data/stock_returns.parquet — SPARSE (one row per (Date, Position) with a real return; a
missing day is absent, never a synthesized zero — see stock_returns_from_prices's docstring for
the full disclosure: zero-fill/coverage semantics belong to the cube/API, not this frame).
"""
from __future__ import annotations
import pathlib
import pandas as pd

from barra_build_frames import stooq_daily, stock_returns_from_prices, _pull_map, DATA_DIR

OUT = pathlib.Path(__file__).resolve().parent.parent / "data"


def build_stock_returns(securities: pd.DataFrame, price_fn=stooq_daily) -> pd.DataFrame:
    """Pull one price series per Ticker (thread-pooled, disk-cached) and derive daily simple
    returns. `price_fn` is injectable for tests (avoids the network)."""
    prices = _pull_map(price_fn, securities["Ticker"].unique(), "stock_returns")
    return stock_returns_from_prices(securities, prices)


if __name__ == "__main__":
    sec_p = DATA_DIR / "securities.parquet"
    if not sec_p.exists():
        raise SystemExit(f"missing {sec_p} -- run barra_build_frames.py first")
    sec = pd.read_parquet(sec_p)[["Position", "Ticker"]]
    print(f"stock_returns: pulling prices for {len(sec)} names "
          f"(reusing the builder's disk cache under tmp/) ...", flush=True)
    df = build_stock_returns(sec)
    n_pos = df["Position"].nunique()
    print(f"stock_returns: {len(df):,} rows, {n_pos:,}/{len(sec)} names priced "
          f"({n_pos / len(sec):.1%} coverage), "
          f"{df['Date'].min().date()} .. {df['Date'].max().date()}" if len(df) else
          "stock_returns: 0 rows -- no prices resolved")
    OUT.mkdir(exist_ok=True)
    df.to_parquet(OUT / "stock_returns.parquet", index=False)
    print(f"wrote {OUT / 'stock_returns.parquet'}")
