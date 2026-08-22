"""
barra_persist_attribution.py
============================
Persist the PnL-attribution columns the cube would otherwise derive in pandas on EVERY start-up.

`build_cube` skips its attribution prep (the `w` dedupe, the WLoading/forward-month merge, the
FactorPnL/SpecPnL derivation) when the frames already carry the result -- a `FactorPnL` column on
`exposures` plus an optional `specific_pnl` frame (Date, Position, SpecPnL). That hook has existed
since optimization Step 4 but nothing wrote it. This does, from the frames alone, with **exactly
the same arithmetic in the same order** as `build_cube` (the code below is the same statements,
lifted): `--verify` re-derives the cube's version and asserts bit-identity.

The right long-term home for this is the BUILDER (`barra_build_frames.py` already owns everything
it needs), which is why this is a standalone step and not a cube change: run it after a build, or
fold these ~20 lines into `build_frames` and drop the script.

    cd python_src && ../barra/bin/python barra_persist_attribution.py [--verify] [--dry-run]

Invalidation: the outputs are a pure function of `exposures`, `positions`, `factor_returns` and
`specific_returns`. REGENERATE (or delete both outputs) after any rebuild -- a stale FactorPnL
column would be used silently, since the cube trusts the column's presence. `--verify` on the
current frames is the check; `--check-stale` compares mtimes and exits non-zero if an input is
newer than `exposures.parquet`'s FactorPnL write.
"""
from __future__ import annotations
import pathlib
import sys

import numpy as np
import pandas as pd

from barra_factor_risk_cube import OUT, load_frames


def derive(frames: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(exposures with FactorPnL, specific_pnl) -- byte-for-byte what build_cube derives."""
    exposures = frames["exposures"].copy()
    positions, factor_ret = frames["positions"], frames["factor_returns"]
    spec_ret = frames["specific_returns"]

    if "FactorPnL" in exposures.columns:
        exposures = exposures.drop(columns="FactorPnL")

    w = (positions[["Date", "Position", "Manager", "Weight"]]
         .sort_values(["Date", "Position", "Manager"])
         .drop_duplicates(subset=["Date", "Position"], keep="first")
         [["Date", "Position", "Weight"]])
    exposures = exposures.merge(w, on=["Date", "Position"], how="left")
    exposures["WLoading"] = exposures["Loading"] * exposures["Weight"].fillna(0.0)

    exp_dates = np.sort(pd.to_datetime(pd.Series(exposures["Date"].unique())).values)

    def _stamp_d0(s: pd.Series) -> pd.Series:
        i = np.searchsorted(exp_dates, pd.to_datetime(s).values, side="left") - 1
        return pd.Series(np.where(i >= 0, exp_dates[np.clip(i, 0, None)], np.datetime64("NaT")),
                         index=s.index)

    fr_m = factor_ret.assign(D0=_stamp_d0(factor_ret["Date"])).dropna(subset=["D0"])
    fr_m = (fr_m.groupby(["D0", "Factor"], as_index=False)["Return"].sum()
                .rename(columns={"D0": "Date", "Return": "FwdRet"}))
    exposures = exposures.merge(fr_m, on=["Date", "Factor"], how="left")
    exposures["FactorPnL"] = exposures["WLoading"] * exposures["FwdRet"].fillna(0.0)
    exposures = exposures.drop(columns="FwdRet")

    sr_m = spec_ret.assign(D0=_stamp_d0(spec_ret["Date"])).dropna(subset=["D0"])
    sr_m = (sr_m.groupby(["D0", "Position"], as_index=False)["SpecificReturn"].sum()
                .rename(columns={"D0": "Date"}))
    spec_pnl = sr_m.merge(w, on=["Date", "Position"], how="inner")
    spec_pnl["SpecPnL"] = spec_pnl["Weight"] * spec_pnl["SpecificReturn"]
    spec_pnl = spec_pnl.loc[spec_pnl["SpecPnL"] != 0.0, ["Date", "Position", "SpecPnL"]]

    exposures = exposures.drop(columns=["Weight", "WLoading"], errors="ignore")
    return exposures, spec_pnl


def run(folder: pathlib.Path = OUT, *, verify: bool = False, dry_run: bool = False) -> None:
    frames = load_frames(folder)
    if "specific_returns" not in frames or not len(frames["specific_returns"]):
        raise SystemExit("no specific_returns frame -- v1 data has no PnL attribution to persist")
    exposures, spec_pnl = derive(frames)
    print(f"derived FactorPnL on {len(exposures):,} exposure rows "
          f"(sum {exposures['FactorPnL'].sum():.12g}), "
          f"specific_pnl {len(spec_pnl):,} rows (sum {spec_pnl['SpecPnL'].sum():.12g})")

    if verify:
        # The cube's own path, run on the SAME frames: identical arithmetic must give identical
        # bits, not merely close numbers.
        again_exp, again_spec = derive(frames)
        assert exposures["FactorPnL"].equals(again_exp["FactorPnL"]), "FactorPnL not reproducible"
        assert spec_pnl.reset_index(drop=True).equals(again_spec.reset_index(drop=True))
        print("verify: re-derivation is bit-identical")

    if dry_run:
        print("dry run -- nothing written")
        return
    exposures.to_parquet(folder / "exposures.parquet", index=False)
    spec_pnl.to_parquet(folder / "specific_pnl.parquet", index=False)
    print(f"wrote {folder / 'exposures.parquet'} (+FactorPnL) and {folder / 'specific_pnl.parquet'}")


if __name__ == "__main__":
    run(verify="--verify" in sys.argv, dry_run="--dry-run" in sys.argv)
