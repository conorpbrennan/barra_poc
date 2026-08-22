"""barra_refresh_positions_mv.py — rewrite the MV column of data/positions.parquet in DOLLARS.

One-off after the 13F unit fix in barra_build_frames._parse_infotable (2026-08-22): values filed
before 2023-01-03 were in $ thousands and the positions frame carried them raw. This re-pulls every
manager's 13F from the warm HTTP cache, rebuilds the per-(Date, Book, Position) MV with the same
per-book as-of join the builder uses, and overwrites ONLY `MV` on the existing frame — Weight and
ADV are untouched, the row set must match exactly (asserted), every other parquet is unchanged.
A full rebuild (~60 min) does the same thing; this is the 5-minute path.

    cd python_src && ../barra/bin/python barra_refresh_positions_mv.py
"""
from __future__ import annotations
import sys, time
import numpy as np
import pandas as pd
import barra_build_frames as B

def main() -> int:
    t0 = time.perf_counter()
    path = B.DATA_DIR / "positions.parquet"
    pos = pd.read_parquet(path)
    cal = pd.date_range(B.START, B.END, freq="ME")
    ckpt = B.DATA_DIR.parent / "tmp" / "refresh_mv_pos13f.parquet"   # the 35-min parse, once
    if ckpt.exists():
        pos13f = pd.read_parquet(ckpt)
        print(f"  parsed filings from checkpoint {ckpt.name}: {len(pos13f):,} rows", flush=True)
    else:
        frames = []
        for mgr in B.MANAGERS:
            if B.ACTIVE_MANAGERS is not None and mgr["book"] not in B.ACTIVE_MANAGERS:
                continue
            kept, _ = B._pull_manager_positions(mgr)
            frames.append(kept)
            print(f"  {mgr['book']}: {len(kept):,} rows", flush=True)
        pos13f = pd.concat(frames, ignore_index=True)
        pos13f.to_parquet(ckpt, index=False)
    # CUSIP -> FIGI from the builder's crosswalk CACHE only: a CUSIP with no positive resolution
    # has no row in positions.parquet, so re-querying OpenFIGI for the 25k unresolved ones (two
    # hours unauthenticated) would change nothing here.
    xc = B._load_xwalk()
    xw = pd.DataFrame([{"cusip": c, "figi": v.get("figi")} for c, v in xc.items() if v and v.get("figi")])
    # ...restricted to the BUILT universe (securities.parquet), as the builder's `sec` join is. A
    # filing whose every name resolves to a FIGI outside the universe must stay invisible: let it
    # into the per-book as-of calendar and it shadows the previous filing for the months after it
    # (Lincoln 2024-07 onward did exactly that on the first run — 12,825 rows went missing).
    sec = pd.read_parquet(B.DATA_DIR / "securities.parquet")
    xw = xw[xw["figi"].isin(set(sec["Position"]))]
    print(f"  crosswalk cache: {len(xw):,} resolved cusips in the built universe", flush=True)
    p = pos13f.merge(xw[["cusip", "figi"]], on="cusip", how="inner")
    p = (p.groupby(["Book", "filing_date", "figi"], as_index=False)["value"].sum()
           .rename(columns={"figi": "Position", "value": "MV"}))
    parts = []
    for book, pb in p.groupby("Book"):
        filings_b = pd.DataFrame({"filing_date": np.sort(pb["filing_date"].unique())})
        cal_b = pd.merge_asof(pd.DataFrame({"Date": cal}), filings_b,
                              left_on="Date", right_on="filing_date", direction="backward")
        parts.append(cal_b.dropna(subset=["filing_date"]).merge(pb, on="filing_date"))
    new = pd.concat(parts, ignore_index=True)[["Date", "Book", "Position", "MV"]]
    merged = pos.drop(columns=["MV"]).merge(new, on=["Date", "Book", "Position"], how="left")
    missing = merged["MV"].isna().sum()
    extra = len(new) - len(merged.dropna(subset=["MV"]).drop_duplicates(["Date", "Book", "Position"]))
    print(f"rows {len(pos):,} -> matched {len(merged) - missing:,}; unmatched {missing:,}; "
          f"new-only {extra:,}", flush=True)
    if missing or len(merged) != len(pos):
        print("ABORT: row set differs from the existing frame — run a full build instead", flush=True)
        return 1
    # the check that matters: pre-2023 MV is now ~1000x the old column, post-2023 identical
    old = pos.set_index(["Date", "Book", "Position"])["MV"]
    ratio = (merged.set_index(["Date", "Book", "Position"])["MV"] / old).replace([np.inf], np.nan)
    pre = ratio[ratio.index.get_level_values("Date") < "2023-02-01"].dropna()
    post = ratio[ratio.index.get_level_values("Date") >= "2023-04-01"].dropna()
    print(f"MV ratio new/old: pre-2023 median {pre.median():.0f} (n={len(pre):,}), "
          f"post-2023 median {post.median():.3f} (n={len(post):,})", flush=True)
    merged = merged[["Date", "Book", "Position", "Weight", "MV", "ADV"]]
    merged.to_parquet(path, index=False)
    s = merged[(merged.Book == "Soros") & (merged.Date == "2026-06-30")]
    print(f"wrote {path} in {time.perf_counter() - t0:.0f}s; Soros 2026-06-30 MV ${s.MV.sum()/1e9:.3f}bn", flush=True)
    return 0

if __name__ == "__main__":
    sys.exit(main())
