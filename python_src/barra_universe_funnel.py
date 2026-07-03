"""
barra_universe_funnel.py
========================
Phase 2 of the universe diagnostics (see docs/universe-diagnostics-plan.md): the monthly DATA-QUALITY
FILTRATION FUNNEL that carves a clean estimation universe out of the point-in-time S&P 500.

Pre-filter population (LOCKED): the S&P 500 as-of each month-end, read point-in-time from the hanshof
change log (Phase 1's Track-A source) — survivorship-free, index component = S&P 500. We do NOT use a
broader index, because only the S&P 500 has free PIT membership (the bitemporal point Chris flagged).

Each name is run through a fixed stack of filters and tagged with the FIRST stage that drops it (or
"survived"). All of Chris's filters appear as stages; the data each needs is computed point-in-time
from the builder's cached prices/fundamentals + the exposures frame:

    listing/sec-type -> size -> history -> trading frequency -> liquidity/ADV -> completeness -> stability buffer

  size              min market cap            close x shares (XBRL, as-of the filing's filed date)
  history           min trading-day history   length of the daily price series up to the month-end
  trading frequency min % of days traded      non-zero-volume days in the trailing window
  liquidity/ADV     min dollar ADV            trailing mean of close x volume
  completeness      >= N of 10 descriptors    non-null style loadings in the exposures frame
  stability buffer  enter/exit hysteresis     ADV-percentile band, carried across months (anti-churn)

Two of Chris's filters can't be done honestly on free data and appear as DISCLOSED, INERT stages that
drop nobody: **free float** (no free float source) and **confirmed-M&A-target removal** (needs deal
data). Listing/sec-type is effectively pass-through here — S&P 500 membership already guarantees a
primary common listing.

NB the funnel is near-flat by design: the S&P 500 is a committee-curated set, so the filters confirm
the input is clean rather than carving much away (exactly as Chris predicted). The per-stage drop
counts prove each filter is wired and evaluated. PIT S&P 500 members not present in the built universe
(delisted names we never pulled) are tagged "data unavailable" — shown, not silently dropped.

Thresholds live in repo-root universe_filters.json (documented + tunable). Pure logic (the filter
predicates, the buffer hysteresis, aggregation) is split out for unit tests. Like the builder, this is
a precompute step; it writes data/universe_funnel.parquet and the /funnel endpoint only reads it.

CLI:  cd python_src && ../barra/bin/python barra_universe_funnel.py
"""
from __future__ import annotations
import json
import pathlib

import numpy as np
import pandas as pd

from barra_build_frames import stooq_daily, fundamentals
import barra_universe_membership as um

OUT = pathlib.Path(__file__).resolve().parent.parent / "data"
ARTIFACT = OUT / "universe_funnel.parquet"
CONFIG = pathlib.Path(__file__).resolve().parent.parent / "universe_filters.json"

STYLE = ["Beta", "Momentum", "Size", "Value", "MegaCap", "RateBeta",
         "Leverage", "Liquidity", "ResidVol", "EarnYield", "NonLinSize"]
# fixed funnel order; the first stage a name fails is its verdict.
STAGES = ["listing", "size", "history", "trading frequency", "liquidity", "completeness",
          "stability buffer"]
DEFAULT_CFG = {
    "min_mcap": 1e8, "min_hist_days": 252, "min_adv": 1e6, "min_trade_freq": 0.9,
    "min_descriptors": 6, "allowed_sec_types": ["Common"],
    "buffer": {"metric": "adv", "enter_pctile": 5, "exit_pctile": 2},
    "unavailable_stages": ["free float", "confirmed M&A removal"],
}


def load_cfg() -> dict:
    cfg = dict(DEFAULT_CFG)
    if CONFIG.exists():
        cfg.update({k: v for k, v in json.loads(CONFIG.read_text()).items() if not k.startswith("_")})
    return cfg


# --------------------------------------------------------------------------- pure logic (unit-tested)
def first_failing_stage(m: dict, cfg: dict) -> str | None:
    """The first filter a name fails (its verdict), or None if it clears stages 1-6. A name we can't
    measure at all (no price history, or no share count for market cap) returns "data unavailable" —
    a disclosed gap, NOT a filter verdict — so the per-filter drop counts only ever reflect genuine
    threshold failures, never missing data. The stability buffer is a separate cross-month pass."""
    def bad(x):
        return x is None or (isinstance(x, float) and np.isnan(x))
    if m.get("sec_type") not in cfg["allowed_sec_types"]:
        return "listing"
    if bad(m.get("mcap")) or not m.get("hist_days"):          # unmeasurable -> disclosed gap
        return "data unavailable"
    if m["mcap"] < cfg["min_mcap"]:
        return "size"
    if m["hist_days"] < cfg["min_hist_days"]:
        return "history"
    if bad(m.get("trade_freq")) or m["trade_freq"] < cfg["min_trade_freq"]:
        return "trading frequency"
    if bad(m.get("adv")) or m["adv"] < cfg["min_adv"]:
        return "liquidity"
    if bad(m.get("n_descriptors")) or m["n_descriptors"] < cfg["min_descriptors"]:
        return "completeness"
    return None


def buffer_members(adv_pctiles: dict, prev: set, enter: float, exit: float) -> set:
    """Stability-buffer hysteresis: a name is in if it clears the (high) ENTER percentile, OR it was
    in last month and still clears the (lower) EXIT percentile. Stops names at one cut-off flipping in
    and out month to month. `adv_pctiles` maps id -> ADV percentile (0-100) among this month's stage-6
    survivors."""
    out = set()
    for k, p in adv_pctiles.items():
        if p >= enter or (k in prev and p >= exit):
            out.add(k)
    return out


def funnel_counts(detail_month: pd.DataFrame, cfg: dict) -> dict:
    """Per-month waterfall: population in, then drops per stage (in funnel order), then survivors.
    Counts reconcile: population = survivors + Σ stage drops + data-unavailable."""
    d = detail_month
    drops = {s: int((d["stage_dropped"] == s).sum()) for s in STAGES}
    return {
        "population": int(len(d)),
        "data_unavailable": int((d["stage_dropped"] == "data unavailable").sum()),
        "drops": drops,
        "survivors": int(d["survived"].sum()),
        "held_survivors": int((d["survived"] & d["held"]).sum()),
    }


# --------------------------------------------------------------------------- metric computation
def _shares_asof(funda: pd.DataFrame, when: pd.Timestamp):
    """Point-in-time shares outstanding: latest XBRL value filed on/before `when`."""
    if funda is None or "Shares" not in funda or funda.empty:
        return None
    s = funda.dropna(subset=["Shares"])
    s = s[pd.to_datetime(s["filed"]) <= when]
    return float(s["Shares"].iloc[-1]) if len(s) else None


def name_metrics(px: pd.DataFrame, funda: pd.DataFrame, months: pd.DatetimeIndex) -> pd.DataFrame:
    """Per-month PIT metrics for one name: hist_days, adv (63d $ vol), trade_freq (63d), mcap.
    All read as-of each month-end from the daily series, so each is point-in-time."""
    if px is None or px.empty:
        return pd.DataFrame(index=months)
    px = px.sort_index()
    dv = px["Close"] * px["Volume"]
    adv = dv.rolling(63, min_periods=20).mean()
    tf = (px["Volume"].fillna(0) > 0).rolling(63, min_periods=20).mean()
    idx = px.index.searchsorted(months, side="right")          # #rows up to each month-end
    rows = []
    for D, i in zip(months, idx):
        if i == 0:
            rows.append({"hist_days": 0, "close": np.nan, "adv": np.nan, "trade_freq": np.nan})
            continue
        rows.append({"hist_days": int(i),
                     "close": float(px["Close"].iloc[i - 1]),
                     "adv": float(adv.iloc[i - 1]) if not np.isnan(adv.iloc[i - 1]) else np.nan,
                     "trade_freq": float(tf.iloc[i - 1]) if not np.isnan(tf.iloc[i - 1]) else np.nan})
    out = pd.DataFrame(rows, index=months)
    out["shares"] = [(_shares_asof(funda, D) or np.nan) for D in months]
    out["mcap"] = out["close"] * out["shares"]
    return out


# --------------------------------------------------------------------------- build
def run(write: bool = True) -> dict:
    cfg = load_cfg()
    print("[funnel] loading frames + PIT S&P 500 history ...", flush=True)
    sec = pd.read_parquet(OUT / "securities.parquet")
    exp = pd.read_parquet(OUT / "exposures.parquet")
    pos = pd.read_parquet(OUT / "positions.parquet")
    hist = um.load_sp500_history()

    months = pd.DatetimeIndex(sorted(pd.to_datetime(exp["Date"].unique())))
    # descriptor completeness per (Date, Position): count of non-null style loadings
    ndesc = (exp[exp["Factor"].isin(STYLE)].groupby(["Date", "Position"]).size()
             .rename("n_descriptors").reset_index())
    ndesc_map = {(pd.Timestamp(d), p): int(n) for d, p, n in
                 zip(ndesc["Date"], ndesc["Position"], ndesc["n_descriptors"])}
    held_map = {(pd.Timestamp(d), p) for d, p in zip(pos["Date"], pos["Position"])}

    # in-universe S&P 500 names = securities whose canon ticker is ever in the PIT S&P 500
    ever = set().union(*[m for _, m in hist]) if hist else set()
    sec = sec.assign(ck=sec["Ticker"].map(um.canon))
    cand = sec[sec["ck"].isin(ever)].copy()
    print(f"[funnel] months={len(months)}  S&P500-ever tickers={len(ever)}  "
          f"in-universe candidates={len(cand)}", flush=True)

    # per-name monthly metrics (warm cache: these names were pulled by the build)
    print("[funnel] computing per-name PIT metrics ...", flush=True)
    metrics = {}
    for _, r in cand.iterrows():
        px = stooq_daily(r["Ticker"])
        fa = fundamentals(int(r["CIK"])) if pd.notna(r["CIK"]) else None
        metrics[r["Position"]] = name_metrics(px, fa, months)

    enter = cfg["buffer"]["enter_pctile"]; exit = cfg["buffer"]["exit_pctile"]
    prev: set = set()
    rows = []
    for D in months:
        # PIT S&P 500 membership at D, by canon ticker
        members = next((mm for d, mm in reversed(hist) if d <= D), frozenset())
        month_cand = cand[cand["ck"].isin(members)]
        present = set(month_cand["Position"])
        # data-unavailable: PIT members we have no built data for (delisted/never pulled)
        n_members = len(members)
        n_present = len(present)

        # stage 1-6 verdicts
        survivors16 = {}
        recs = []
        for _, r in month_cand.iterrows():
            P = r["Position"]; mt = metrics[P].loc[D]
            m = {"sec_type": "Common",          # S&P 500 => primary common listing
                 "mcap": mt["mcap"], "hist_days": mt["hist_days"],
                 "trade_freq": mt["trade_freq"], "adv": mt["adv"],
                 "n_descriptors": ndesc_map.get((D, P), 0)}
            stage = first_failing_stage(m, cfg)
            rec = {"month": D, "position": P, "ticker": r["Ticker"], "issuer": r["Issuer"],
                   "sec_type": "Common", "mcap": _f(mt["mcap"]), "hist_days": int(mt["hist_days"]),
                   "trade_freq": _f(mt["trade_freq"]), "adv": _f(mt["adv"]),
                   "n_descriptors": int(m["n_descriptors"]),
                   "held": (D, P) in held_map, "stage_dropped": stage}
            recs.append(rec)
            if stage is None:
                survivors16[P] = mt["adv"]

        # stability buffer (ADV-percentile hysteresis) over stage-1-6 survivors
        advs = {p: a for p, a in survivors16.items() if a is not None and not np.isnan(a)}
        if advs:
            ser = pd.Series(advs)
            pct = ser.rank(pct=True) * 100.0
            keep = buffer_members(pct.to_dict(), prev, enter, exit)
        else:
            keep = set()
        prev = keep

        for rec in recs:
            if rec["stage_dropped"] is None:
                if rec["position"] in keep:
                    rec["stage_dropped"] = None; rec["survived"] = True; rec["adv_pctile"] = None
                else:
                    rec["stage_dropped"] = "stability buffer"; rec["survived"] = False
                    rec["adv_pctile"] = None
            else:
                rec["survived"] = False; rec["adv_pctile"] = None
            rows.append(rec)

        # add the data-unavailable PIT members (no row above) for honest counts
        # (count only; names unknown without a ticker map for delisted members)
        for _ in range(max(0, n_members - n_present)):
            rows.append({"month": D, "position": None, "ticker": None, "issuer": None,
                         "sec_type": None, "mcap": None, "hist_days": None, "trade_freq": None,
                         "adv": None, "n_descriptors": None, "held": False,
                         "stage_dropped": "data unavailable", "survived": False, "adv_pctile": None})

    detail = pd.DataFrame(rows)
    if write:
        OUT.mkdir(parents=True, exist_ok=True)
        detail.to_parquet(ARTIFACT, index=False)
        print(f"[funnel] wrote {ARTIFACT}  ({len(detail)} name-month rows)", flush=True)

    # summary on the latest month
    last = months[-1]
    fc = funnel_counts(detail[detail["month"] == last], cfg)
    print(f"\n[funnel] latest month {last.date()}  (population {fc['population']}, "
          f"{fc['data_unavailable']} data-unavailable)")
    print(f"    survivors: {fc['survivors']}  ({fc['held_survivors']} held)")
    for s in STAGES:
        if fc["drops"][s]:
            print(f"    dropped @ {s:18s}: {fc['drops'][s]}")
    return {"detail": detail, "cfg": cfg, "latest": str(last.date()), "latest_counts": fc}


def _f(x):
    return None if (x is None or (isinstance(x, float) and np.isnan(x))) else float(x)


if __name__ == "__main__":
    run()
