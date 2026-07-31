"""
barra_factor_risk_cube.py
=========================
Atoti factor-risk cube (two-block historical model) + unified scenario/stress layer.

Pairs with barra_build_frames.py. Seven input frames, plus one optional 8th (Phase 2):
  exposures        (Date, Position, Factor) -> Loading        <-- GRANULAR LEAF
  positions        (Date, Book, Position)   -> Weight, MV      <-- multi-manager 13F overlay
  securities       (Position)               -> Ticker, CIK, CUSIP, Issuer, Sector, Country
  factor_meta      (Factor)                 -> FactorGroup
  factor_returns   (Date, Factor)           -> Return          <-- source of every scenario set
  specific_var     (Date, Position)         -> SpecificVar     <-- diagonal block
  specific_returns (Date, Position)         -> SpecificReturn  <-- daily WLS residual (attribution);
                                                                    v2-only, optional
  managers         (Book)                   -> CIK, EntityName, FirmType, ETP-drop disclosure;
                                                                    optional, Phase 2 entity dim

ALL THREE SCENARIO MODES ARE ONE OPERATION:  dPnL = sum_k x_k * df_k
  x_k = aggregated factor exposure (the cube's "Net exposure" at the Factor level)
  df_k = a factor-shock vector that DIFFERS ONLY BY SOURCE:
     * HistFull      -> the full factor-return history          (historical simulation VaR/ES)
     * <event>       -> factor returns over a past window       (historical event replay)
     * Hypo:*        -> hand-set sigma-shocks, length-1 vector  (hypothetical stress)
The "one switch" is the ScenarioSet hierarchy: slice it, and the same measure gives that mode.
"""
from __future__ import annotations
import pathlib
import numpy as np
import pandas as pd
import atoti as tt

OUT = pathlib.Path(__file__).resolve().parent.parent / "data"
FRAME_NAMES = ["exposures", "positions", "securities", "factor_meta", "factor_returns", "specific_var"]
# specific_returns: v2-only (PnL attribution); v1 data degrades gracefully.
# managers: multi-manager entity metadata (Phase 2, keyed on Book); absent on any build before it
# existed (incl. all v1 data) -- degrades gracefully, no entity dimension, nothing else affected.
OPTIONAL_FRAMES = ["specific_returns", "managers"]

# Historical event windows to replay (must fall inside the loaded sample; pre-2016 events need a
# longer factor-return history -- splice published style-factor returns for those windows).
EVENT_WINDOWS = {
    "Evt:COVID2020":   ("2020-02-01", "2020-05-31"),
    "Evt:Rates2022":   ("2022-01-01", "2022-10-31"),
    "Evt:Selloff2018": ("2018-10-01", "2018-12-31"),
}
# Hypothetical shocks in units of each factor's sigma; unlisted factors shocked 0.
HYPO_SHOCKS = {
    "Hypo:ValueRotation": {"Value": +2.0, "Momentum": -2.0},
    "Hypo:RiskOff":       {"Beta": -2.0, "ResidVol": +2.0, "Momentum": +1.0},
    "Hypo:MomentumCrash": {"Momentum": -3.0},
}


def load_frames(folder: pathlib.Path = OUT) -> dict[str, pd.DataFrame]:
    miss = [n for n in FRAME_NAMES if not (folder / f"{n}.parquet").exists()]
    if miss:
        raise FileNotFoundError(f"Run barra_build_frames.py first; missing: {miss}")
    frames = {n: pd.read_parquet(folder / f"{n}.parquet") for n in FRAME_NAMES}
    for n in OPTIONAL_FRAMES:
        if (folder / f"{n}.parquet").exists():
            frames[n] = pd.read_parquet(folder / f"{n}.parquet")
    return frames


def _pit_month_ends(wide: pd.DataFrame, min_obs: int = 60) -> list[pd.Timestamp]:
    """The month-ends that get a PIT:* truncated-history set: every calendar month-end covered
    by the factor-return panel with at least `min_obs` prior trading days (a PIT set needs
    enough history for the sample estimators to mean anything)."""
    ends = pd.date_range(wide.index.min(), wide.index.max(), freq="ME")
    return [t for t in ends if (wide.index <= t).sum() >= min_obs]


def build_scenarios(factor_ret: pd.DataFrame, style: list[str]) -> pd.DataFrame:
    """One table keyed (ScenarioSet, Factor) -> ShockVec. Vectors share length WITHIN a set."""
    wide = (factor_ret[factor_ret["Factor"].isin(style)]
            .pivot(index="Date", columns="Factor", values="Return").dropna(how="any").sort_index())
    vol = wide.std()
    rows = []
    # 1) full historical simulation
    for f in wide.columns:
        rows.append({"ScenarioSet": "HistFull", "Factor": f, "ShockVec": wide[f].to_numpy().tolist()})
    # 2) historical event replay
    for name, (a, b) in EVENT_WINDOWS.items():
        w = wide.loc[a:b]
        if len(w) == 0:
            continue
        for f in wide.columns:
            rows.append({"ScenarioSet": name, "Factor": f, "ShockVec": w[f].to_numpy().tolist()})
    # 3) hypothetical sigma-shocks (length-1 vectors)
    for name, shock in HYPO_SHOCKS.items():
        for f in wide.columns:
            rows.append({"ScenarioSet": name, "Factor": f,
                         "ShockVec": [float(shock.get(f, 0.0)) * float(vol[f])]})
    # 4) POINT-IN-TIME sets: history truncated at each month-end ("PIT:YYYY-MM-DD"). These make
    #    the cube's risk measures honest as-of a date: Model vol at (Date=t, ScenarioSet=PIT:t)
    #    uses only information available at t — the full-history HistFull quietly uses later
    #    data when charted back in time. ~10MB total; /meta hides them from the main dropdown
    #    (served as `pit_sets`). The LAST PIT set is the full panel == HistFull by construction.
    for t in _pit_month_ends(wide):
        w = wide.loc[:t]
        name = f"PIT:{t.date()}"
        for f in wide.columns:
            rows.append({"ScenarioSet": name, "Factor": f, "ShockVec": w[f].to_numpy().tolist()})
    return pd.DataFrame(rows)


def build_scenario_axis(factor_ret: pd.DataFrame, style: list[str]) -> pd.DataFrame:
    """Per ScenarioSet, the ordered DATE AXIS of its shock/P&L vector, as epoch-day ints.

    Mirrors build_scenarios EXACTLY (same `wide`, same windows, same set membership) so that
    index i of a set's ShockVec/PnL vector maps to DateVec[i]. Hypo sets are length-1 vectors
    with no real date -> stamped with the last history date for alignment.
    """
    wide = (factor_ret[factor_ret["Factor"].isin(style)]
            .pivot(index="Date", columns="Factor", values="Return").dropna(how="any").sort_index())
    epoch = pd.Timestamp("1970-01-01")
    def days(idx):
        return [int((pd.Timestamp(d) - epoch).days) for d in idx]
    rows = [{"ScenarioSet": "HistFull", "DateVec": days(wide.index)}]
    for name, (a, b) in EVENT_WINDOWS.items():
        w = wide.loc[a:b]
        if len(w):
            rows.append({"ScenarioSet": name, "DateVec": days(w.index)})
    last = days(wide.index[-1:])                       # length-1 stamp for hypothetical sets
    for name in HYPO_SHOCKS:
        rows.append({"ScenarioSet": name, "DateVec": last})
    for t in _pit_month_ends(wide):                    # PIT sets: axis mirrors the truncation
        rows.append({"ScenarioSet": f"PIT:{t.date()}", "DateVec": days(wide.loc[:t].index)})
    return pd.DataFrame(rows)


def build_cube(frames: dict[str, pd.DataFrame], port: int = 9090):
    exposures, positions = frames["exposures"], frames["positions"]
    securities, factor_meta = frames["securities"], frames["factor_meta"]
    factor_ret, specific = frames["factor_returns"], frames["specific_var"]

    style = [f for f in factor_ret["Factor"].unique() if f != "Market"]   # the 10 style factors (for reporting)
    # INCLUDE Market in the scenarios: it now carries a leaf loading of 1.0 per name (the v2
    # intercept), so the directional market factor return flows through dPnL. x_Market = Σ weights.
    scn_factors = [f for f in factor_ret["Factor"].unique() if f in set(exposures["Factor"]) or f == "Market"]
    scenarios = build_scenarios(factor_ret, scn_factors)
    scn_axis = build_scenario_axis(factor_ret, scn_factors)   # date axis dual of the shock/P&L vectors

    # Leaf products: `Net exposure` is now MEASURE-LEVEL (Loading x the JOINED Positions
    # Weight under an OriginScope) — benchmarked 2026-07-03 on atoti 0.9.15 at parity with the
    # old physical WLoading column on every query incl. the historical ~9s Date x Position
    # pivot (375ms vs 380ms), bit-exact. The point of the switch: the weight is read from the
    # Positions table AT QUERY TIME, so a source-scenario branch overriding Positions rows
    # flows through Net exposure and every measure chained off it (the what-if branch design —
    # docs/cube-measure-opportunities.md #4). The pandas WLoading below remains ONLY as an
    # intermediate for the attribution FactorPnL column (fwd returns baked at load; attribution
    # is deliberately NOT branch-sensitive). Unheld names: Weight join is None -> the product
    # contributes nothing, same as the old fillna(0) column.
    #
    # KNOWN LIMITATION (Phase 2, multi-manager): FactorPnL/SpecPnL below are BOOK-INDEPENDENT --
    # exactly as in the single-book (Soros-only) era -- because they are baked physical columns
    # on tables keyed WITHOUT Book (deliberately: attribution must stay immune to the what-if
    # trades branch, which lives on the Positions table's `scenarios[...]`, so these columns
    # cannot read Positions' live/branchable Weight at query time the way Net exposure does).
    # Making them genuinely per-book would need Book as a SECOND reused hierarchy dual on the
    # same side table alongside Factor (reused from Exposures) -- tried and reverted: every join
    # topology (single edge either way, or a two-edge "diamond" mapping Book via Positions AND
    # Factor via Exposures) left one of the two axes as an unresolvable ambiguous duplicate
    # hierarchy (`Disambiguate 'Book' to ... [('Positions','Book','Book'), ('FactorPnL','Book',
    # 'Book')]`), confirmed empirically, not merely suspected. `w` is deduped below to a SINGLE,
    # deterministic Weight per (Date, Position) (first book alphabetically) purely so the merge
    # can no longer silently create a duplicate-keyed row for a name held by more than one book
    # (a genuine data-corruption risk with the raw multi-book Positions frame) -- but the
    # resulting Factor contribution / Specific PnL / Realized PnL are NOT reliable for per-book
    # analysis when a name is held by more than one book; they read one arbitrary book's weight
    # for that name regardless of which Book is sliced. Disclosed, not silently fixed. Follow-up:
    # either confirm an Atoti-supported way to alias a table's column onto an EXISTING hierarchy
    # from a different table, or build N book-keyed tables in a loop over the active books.
    w = (positions[["Date", "Position", "Book", "Weight"]]
         .sort_values(["Date", "Position", "Book"])
         .drop_duplicates(subset=["Date", "Position"], keep="first")
         [["Date", "Position", "Weight"]])
    exposures = exposures.merge(w, on=["Date", "Position"], how="left")
    exposures["WLoading"] = exposures["Loading"] * exposures["Weight"].fillna(0.0)

    # ---- PnL attribution (Step 15, v2-only): forward-month realized contributions -------------
    # Convention: the row at month-end d0 carries the PnL over the FOLLOWING month (d0, d1] — the
    # month the d0 exposures/weights explain (the regression fits days d0 < t <= d1 on the d0
    # loadings). Daily factor/specific returns are summed arithmetically into that window, so
    # `Factor contribution` is a plain additive column (WLoading x fwd-month factor return) that
    # foots at every level of Factor x Sector x Position, and `Specific PnL` is the as-of weight x
    # fwd-month residual per (Date, Position). Realized PnL = the two summed — an identity.
    spec_ret = frames.get("specific_returns")
    has_attribution = spec_ret is not None and len(spec_ret) > 0
    spec_pnl = None
    if has_attribution:
        exp_dates = np.sort(pd.to_datetime(pd.Series(exposures["Date"].unique())).values)

        def _stamp_d0(s: pd.Series) -> pd.Series:
            """Largest exposure month-end STRICTLY BEFORE each daily date (the window owner)."""
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
    exposures = exposures.drop(columns=["Weight", "WLoading"])
    # NB: the same trick must NOT be applied to specific_var — it is a *joined* table, and a
    # plain SUM over a joined column fans out by fact-row multiplicity (each (Date, Position)
    # specvar row is reached once per factor leaf -> ~10x inflated variance, observed √10
    # jump in Specific vol). Its measure keeps the OriginScope form below.

    managers = frames.get("managers")
    has_managers = managers is not None and len(managers) > 0

    session = tt.Session.start(tt.SessionConfig(port=port))   # pinned so the UI URL survives restarts
    t_exp = session.read_pandas(exposures,  keys={"Date", "Position", "Factor"}, table_name="Exposures")
    t_pos = session.read_pandas(positions,  keys={"Date", "Book", "Position"},   table_name="Positions")
    t_sec = session.read_pandas(securities, keys={"Position"},                   table_name="Securities")
    t_fm  = session.read_pandas(factor_meta, keys={"Factor"},                    table_name="FactorMeta")
    t_sv  = session.read_pandas(specific,   keys={"Date", "Position"},           table_name="SpecificVar")
    t_scn = session.read_pandas(scenarios,  keys={"ScenarioSet", "Factor"},      table_name="Scenarios")
    t_axis = session.read_pandas(scn_axis,  keys={"ScenarioSet"},                table_name="ScenarioAxis")
    t_sr = (session.read_pandas(spec_pnl, keys={"Date", "Position"}, table_name="SpecificPnL")
            if has_attribution else None)
    t_mgr = (session.read_pandas(managers, keys={"Book"}, table_name="Managers")
             if has_managers else None)

    t_exp.join(t_pos, (t_exp["Date"] == t_pos["Date"]) & (t_exp["Position"] == t_pos["Position"]))
    t_exp.join(t_sec, t_exp["Position"] == t_sec["Position"])
    t_exp.join(t_fm,  t_exp["Factor"] == t_fm["Factor"])
    t_exp.join(t_sv,  (t_exp["Date"] == t_sv["Date"]) & (t_exp["Position"] == t_sv["Position"]))
    if t_sr is not None:
        t_exp.join(t_sr, (t_exp["Date"] == t_sr["Date"]) & (t_exp["Position"] == t_sr["Position"]))
    # PARTIAL join: map Factor only -> ScenarioSet becomes a hierarchy (the "switch"). One ShockVec
    # is selected per (ScenarioSet, Factor); exposures fan across sets but Net exposure is unaffected.
    t_exp.join(t_scn, t_exp["Factor"] == t_scn["Factor"])
    t_scn.join(t_axis, t_scn["ScenarioSet"] == t_axis["ScenarioSet"])   # date axis, one row per set
    if t_mgr is not None:
        # Entity metadata for the Book dimension (Phase 2). PARTIAL join on Book only, off the
        # SAME Positions table whose un-mapped Book key already makes Book a hierarchy -- so this
        # degrades cleanly when managers.parquet is absent (v1 data / any pre-Phase-2 build has
        # no entity dimension, nothing else affected).
        t_pos.join(t_mgr, t_pos["Book"] == t_mgr["Book"])

    cube = session.create_cube(t_exp, mode="manual")
    h, l, m = cube.hierarchies, cube.levels, cube.measures

    # Atoti guards a query's accumulated point count; the defaults are intermediate 1,000,000 /
    # transient 10,000,000. The single-book PoC never came near them, but the multi-manager build
    # (exposures 1.6M -> 5.7M leaf rows across 11 books) trips the intermediate limit on ordinary
    # queries -- /dims 500'd on EVERY dimension with
    #   RetrievalResultSizeException [points size before contribution = 1000000, max limit = 1000000]
    # taking the whole Pivot lens down with it. These are guards against runaway memory, not
    # correctness limits, so they are raised to keep the same headroom-to-universe ratio as before
    # (cube RSS measured at 1.1 GB on this dataset, against 62 GB on the box).
    cube.shared_context["queriesResultLimit.intermediateLimit"] = 20_000_000
    cube.shared_context["queriesResultLimit.transientLimit"] = 200_000_000

    h["Security"]  = {"Country": t_sec["Country"], "Sector": t_sec["Sector"],
                      "Issuer": t_sec["Issuer"], "Position": t_exp["Position"]}
    # FLAT position hierarchy for GLOBAL cross-member ranking (tt.rank ranks siblings; under
    # Security a position's siblings are its issuer's positions). Level named PositionR so the
    # plain l["Position"] lookups stay unambiguous; hidden — rank plumbing, not a browse dim.
    h["PositionRank"] = {"PositionR": t_exp["Position"]}
    try:
        h["PositionRank"].visible = False
    except Exception:
        pass
    h["FactorDim"] = {"FactorGroup": t_fm["FactorGroup"], "Factor": t_exp["Factor"]}
    h["Date"]      = {"Date": t_exp["Date"]}
    # Book and ScenarioSet are NOT created manually: they are the un-mapped key columns of the
    # partial joins (Positions, Scenarios). In manual cube mode atoti auto-creates their
    # hierarchies once the mapped key columns (Date, Position, Factor) have hierarchies.
    assert {"Book", "ScenarioSet"} <= {n for _, n in h}, sorted(n for _, n in h)
    # Entity dimension (Phase 2): a SEPARATE hierarchy, not extra levels grafted onto "Book" --
    # deliberately conservative so the pre-existing, auto-created, single-level "Book" hierarchy
    # (and every filter/level lookup against it elsewhere, incl. risk_api.py) is untouched. Manager
    # is 1:1 with Book (same t_pos->t_mgr row), so filtering by either stays consistent. It's never
    # passed to a lift/OriginScope call, exactly like Book itself -- so it always reads whatever
    # book is currently sliced, and is blank/ambiguous at the no-book grand total (same as any
    # other book-scoped attribute; see the grand-total note near Net exposure).
    if t_mgr is not None:
        h["Manager"] = {"FirmType": t_mgr["FirmType"], "EntityName": t_mgr["EntityName"],
                        "CIK": t_mgr["CIK"]}

    # ---- additive exposures (drill/slice; independent of ScenarioSet) -------------------------
    # MEASURE-LEVEL product of the leaf Loading and the JOINED Positions Weight (see the leaf-
    # products note at the top of build_cube): reads the weight at query time, so scenario
    # branches on Positions flow through this and every chained measure. Benchmarked at parity
    # with the old physical column.
    m["Net exposure"] = tt.agg.sum(
        tt.agg.single_value(t_exp["Loading"]) * tt.agg.single_value(t_pos["Weight"]),
        scope=tt.OriginScope({l["Date"], l["Position"], l["Factor"]}))
    m["Net exposure"].formatter = "DOUBLE[0.000]"

    # ---- entity dimension measures (Phase 2): the managers.parquet disclosure fields, read at
    # whatever Book is currently sliced (single_value over the Book-keyed Managers table joined
    # via Positions -- see the Manager hierarchy note above). Blank/ambiguous with no Book slice.
    if t_mgr is not None:
        m["Manager ETP dropped value share"] = tt.agg.single_value(t_mgr["dropped_value_share_latest"])
        m["Manager ETP dropped value share"].formatter = "DOUBLE[0.00%]"
        m["Manager n filings"] = tt.agg.single_value(t_mgr["n_filings"])
        m["Manager n positions"] = tt.agg.single_value(t_mgr["n_positions_distinct"])

    # ---- ONE scenario engine: aggregate exposure to K factor scalars, scale each factor's shock
    #      vector once, sum K vectors -> P&L vector for the sliced ScenarioSet. (Confirm tt.array.*
    #      helper names in your SDK; validate the OriginScope pins step-2 to one vector per Factor.)
    #      NB: query risk measures sliced to a SINGLE ScenarioSet -- vector lengths differ across sets.
    m["Scenario PnL vector"] = tt.agg.sum(
        m["Net exposure"] * tt.agg.single_value(t_scn["ShockVec"]),
        scope=tt.OriginScope({l["Factor"]}))
    m["Scenario mean PnL"]   = tt.array.mean(m["Scenario PnL vector"])     # hypo: the shock P&L; hist: ~0
    m["Scenario VaR 99"]     = -tt.array.quantile(m["Scenario PnL vector"], 0.01)
    m["Scenario worst loss"] = -tt.array.min(m["Scenario PnL vector"])     # worst single scenario
    for k in ("Scenario mean PnL", "Scenario VaR 99", "Scenario worst loss"):
        m[k].formatter = "DOUBLE[0.00%]"
    m["Scenario n"] = tt.array.len(m["Scenario PnL vector"])   # THIS set's vector length

    # ---- extra confidence levels + Expected Shortfall (coherent tail measure) ------------------
    # VaR at 95 / 97.5 alongside the existing 99 (the 95/99 pair reads tail fatness; 97.5 is the
    # FRTB regulatory point). ES (a.k.a. CVaR) is the MEAN loss in the tail BEYOND VaR -- coherent
    # / sub-additive where VaR is not, and the Basel FRTB replacement for VaR. The tail SIZE k
    # scales with each set's OWN vector length n (HistFull 2203 vs COVID 82 vs hypo 1), so k is a
    # MEASURE: k = ceil(alpha * n) worst observations and ES = -mean of the k lowest P&L. n_lowest
    # accepts a measure for n, so one definition serves every ragged scenario set (k >= 1 always:
    # ceil of a positive number; k <= n since alpha < 1, so never indexes past the vector).
    m["Scenario VaR 95"]   = -tt.array.quantile(m["Scenario PnL vector"], 0.05)
    m["Scenario VaR 97.5"] = -tt.array.quantile(m["Scenario PnL vector"], 0.025)
    _k975 = tt.math.ceil(0.025 * m["Scenario n"])     # tail size for 97.5% ES
    _k99  = tt.math.ceil(0.01  * m["Scenario n"])     # tail size for 99% ES
    m["Scenario ES 97.5"] = -tt.array.mean(tt.array.n_lowest(m["Scenario PnL vector"], _k975))
    m["Scenario ES 99"]   = -tt.array.mean(tt.array.n_lowest(m["Scenario PnL vector"], _k99))
    # plain dispersion of the scenario P&L (per-observation sigma, same units as the returns): the
    # non-tail risk number that pairs with VaR/ES and feeds the diversification read below.
    m["Scenario PnL vol"] = tt.array.std(m["Scenario PnL vector"])
    # ---- exceedance rate (ch-08's simple calibration diagnostic, per CELL so it drills) ------
    # share of scenario days beyond ±2 of the cell's own vol. There is no elementwise compare
    # or abs for array measures, but elementwise / IS supported, so the exact 0/1 indicator is
    # positive_values(v−t)/(v−t): 1 above the threshold, 0/negative = 0 below (NaN only if an
    # element equals the threshold EXACTLY — measure-zero on continuous P&L).
    # NB negative_values/positive_values are LENGTH-PRESERVING (zero-fill, not filter) — a
    # len()-based count reads n and is wrong. Normal tails ≈ 4.6%; fatter reads higher.
    # Degenerate (blank) on length-1 Hypo sets (sample vol undefined).
    _v2 = 2.0 * m["Scenario PnL vol"]
    _up = m["Scenario PnL vector"] - _v2
    _dn = m["Scenario PnL vector"] + _v2
    m["Exceedance rate 2s"] = (
        (tt.array.sum(tt.array.positive_values(_up) / _up)
         + tt.array.sum(tt.array.negative_values(_dn) / _dn))
        / m["Scenario n"])
    m["Exceedance rate 2s"].formatter = "DOUBLE[0.00%]"
    for k in ("Scenario VaR 95", "Scenario VaR 97.5", "Scenario ES 97.5",
              "Scenario ES 99", "Scenario PnL vol"):
        m[k].formatter = "DOUBLE[0.00%]"

    # ---- index -> date DUAL of the P&L vector: aligned 1:1 with "Scenario PnL vector". --------
    # DateVec[i] is the date that produced PnL vector[i] (epoch days; one row per set, so
    # single_value is unambiguous when sliced to a single ScenarioSet -- the same constraint
    # the vector measures carry). Lets a caller label the historical-sim / event-replay path
    # and name the worst-loss / VaR-breach dates.
    m["Scenario dates (epoch)"] = tt.agg.single_value(t_axis["DateVec"])

    # date of the WORST scenario (argmin of the P&L vector), computed IN THE CUBE: the index of the
    # minimum read against the date dual. Lets the API report the worst-loss date without any
    # client/numpy argmin — the cube owns it, like Scenario worst loss / VaR 99.
    _worst_idx = tt.array.quantile_index(m["Scenario PnL vector"], 0.0, interpolation="lower")
    m["Scenario worst date (epoch)"] = m["Scenario dates (epoch)"][_worst_idx]

    # the P&L vector SORTED ascending — the empirical loss distribution, computed IN THE CUBE
    # (tt.array.sort). Lets a distribution chart show the full shape (sorted P&L vs percentile)
    # without any client-side binning/counting; the array primitives can't COUNT-per-bin anyway.
    m["Scenario PnL sorted"] = tt.array.sort(m["Scenario PnL vector"])

    # ---- synthetic ScenarioDay dimension: UNPACK the P&L/date vectors into one row per array
    # element WITHOUT exploding the facts — the vector/array stays intact in the cube. A parameter
    # hierarchy of day-indices 0..N-1; its auto index measure picks the element at the CURRENT
    # member, so the same vector-indexing idiom used above (Scenario PnL vector[tail_idx]) projects
    # each day. Put ScenarioDay on a query axis -> the vector becomes a tabular per-day series (a
    # real pivot query: levels=[ScenarioDay] = the book path, +[Sector] = the sector breakout);
    # leave it OFF and every existing vector/scalar measure is byte-for-byte unchanged. N spans the
    # longest set (HistFull); shorter sets index past their length -> null (hide_empty drops them).
    # The calendar DATE rides along as the "Scenario date at day" measure, not a member — event
    # windows span different dates per set, so they can't be shared global members.
    N_days = int(scn_axis["DateVec"].map(len).max())   # longest set (HistFull) sizes the dimension
    cube.create_parameter_hierarchy_from_members(
        "ScenarioDay", list(range(N_days)), index_measure_name="ScenarioDay index")
    _day = m["ScenarioDay index"]
    # Scenario n (this set's vector length) is defined up with the tail measures above.
    # CLAMP the index in-bounds before reading the vector: Atoti errors the WHOLE query on an
    # out-of-range index (not a per-cell null), and shorter sets (COVID=82) are queried under the
    # full-size dimension (HistFull=2203). So index at a safe position (0 past the end) and NULL the
    # value beyond the set's own length — NON EMPTY / hide_empty then drop the surplus members.
    _safe = tt.where(_day < m["Scenario n"], _day, 0)
    _in = _day < m["Scenario n"]                       # this member is a real day of THIS set
    m["Scenario PnL at day"]          = tt.where(_in, m["Scenario PnL vector"][_safe], None)
    m["Scenario date at day (epoch)"] = tt.where(_in, m["Scenario dates (epoch)"][_safe], None)
    # chart-ready markers, ALSO gated by ScenarioDay so a per-day query is null past the set's length
    # (else these index-INDEPENDENT scalars stay non-null for every member and NON EMPTY can't trim
    # the query to its real days). VaR/worst are lifted to BOOK level (tt.total) so the rule/point are
    # the book's, identical whether or not Sector is on the axis; signs are baked for the chart (the
    # loss threshold and worst P&L are negative). Used by the COVID view's two graphs.
    _book_var  = tt.total(m["Scenario VaR 99"], h["Security"], h["FactorDim"], h["PositionRank"])
    _book_loss = tt.total(m["Scenario worst loss"], h["Security"], h["FactorDim"], h["PositionRank"])
    m["Scenario VaR line at day"]           = tt.where(_in, -_book_var, None)
    m["Scenario worst pnl at day"]          = tt.where(_in, -_book_loss, None)
    m["Scenario worst date at day (epoch)"] = tt.where(_in, m["Scenario worst date (epoch)"], None)
    for _mn in ("Scenario PnL at day", "Scenario VaR line at day", "Scenario worst pnl at day"):
        m[_mn].formatter = "DOUBLE[0.00%]"

    # ---- diagonal specific block (additive, scenario-independent) -----------------------------
    # OriginScope + single_value, NOT a columnar SUM: see the fan-out note at the top of
    # build_cube. Cheap regardless — one term per (Date, Position) member in context.
    wgt = tt.agg.single_value(t_pos["Weight"])
    m["Specific variance"] = tt.agg.sum(
        wgt * wgt * tt.agg.single_value(t_sv["SpecificVar"]),
        scope=tt.OriginScope({l["Date"], l["Position"]}))
    m["Specific vol"] = tt.math.sqrt(m["Specific variance"])
    # gross/net book weight from the JOINED Positions Weight (branch-sensitive like everything
    # else; one term per (Date, Position) like Specific variance). In-cube identity: Net weight
    # == Net exposure at Factor=Market (the unit Market loading makes x_Market = Σ weights).
    m["Net weight"] = tt.agg.sum(wgt, scope=tt.OriginScope({l["Date"], l["Position"]}))
    m["Gross weight"] = tt.agg.sum(tt.math.abs(wgt),
                                   scope=tt.OriginScope({l["Date"], l["Position"]}))
    for _mn in ("Net weight", "Gross weight"):
        m[_mn].formatter = "DOUBLE[0.000]"
    # ---- model vol: THE reference risk number (2026-07-03), sigma = sqrt(x'Fx + w'dw) ---------
    # Factor half = std of the scenario P&L vector (atoti 'sample' mode == np.cov ddof=1), so
    # sliced to HistFull this IS the model sigma on the same full-history covariance the API uses
    # (_risk_from_weights / _euler_contributions tie out to float precision). On Evt:* it reads
    # as the window (regime) vol; on length-1 Hypo:* the sample std is degenerate — blank.
    # Computed per cell (each slice's own vector + own specific block), so it drills by
    # sector/name/factor like the VaR measures.
    m["Model vol"] = tt.math.sqrt(m["Scenario PnL vol"] ** 2 + m["Specific variance"])
    m["Model vol"].formatter = "DOUBLE[0.00%]"
    # approximate total tail: factor scenario VaR with an independent idiosyncratic tail (z=2.326)
    m["Total VaR 99"] = tt.math.sqrt(m["Scenario VaR 99"] * m["Scenario VaR 99"]
                                     + (2.326 * m["Specific vol"]) ** 2)
    m["Specific vol"].formatter = "DOUBLE[0.00%]"
    m["Total VaR 99"].formatter = "DOUBLE[0.00%]"
    # Expected-Shortfall analogue of Total VaR: factor-ES combined in quadrature with the
    # idiosyncratic tail. For a normal tail the ES97.5 multiplier is phi(z)/(1-a) = 2.338 (~ the
    # 2.326 used for 99% VaR -- ES97.5 == VaR99 under normality), applied to the specific vol.
    m["Total ES 97.5"] = tt.math.sqrt(m["Scenario ES 97.5"] * m["Scenario ES 97.5"]
                                      + (2.338 * m["Specific vol"]) ** 2)
    m["Total ES 97.5"].formatter = "DOUBLE[0.00%]"

    # ---- PnL attribution measures (Step 15; only when the 7th frame was built) ----------------
    # All three are ADDITIVE scalars (same class as Net exposure — no ragged vectors), reading the
    # forward-month convention set at load: the value at Date d0 is the PnL over (d0, d1].
    #   Factor contribution — a plain columnar SUM of the precomputed leaf product
    #                         w_i·L_ik·f_k(fwd month); Σ over names = x_k·f_k, so Factor→Name
    #                         drills foot exactly.
    #   Specific PnL        — w_i·ε_i(fwd month) per (Date, Position) via OriginScope+single_value
    #                         (a joined table: a plain SUM would fan out per factor leaf, the same
    #                         trap the SpecificVar note above flags). By FACTOR it fans out —
    #                         specific risk has no factor — use it in by-name/book views.
    #   Realized PnL        — the identity Factor contribution + Specific PnL.
    if t_sr is not None:
        # KNOWN LIMITATION (Phase 2, multi-manager): book-independent, see the `w` note near the
        # top of build_cube -- reads ONE arbitrary book's weight for a name held by >1 book.
        m["Factor contribution"] = tt.agg.sum(t_exp["FactorPnL"])
        m["Specific PnL"] = tt.agg.sum(
            tt.agg.single_value(t_sr["SpecPnL"]),
            scope=tt.OriginScope({l["Date"], l["Position"]}))
        m["Realized PnL"] = m["Factor contribution"] + m["Specific PnL"]
        for _mn in ("Factor contribution", "Specific PnL", "Realized PnL"):
            m[_mn].formatter = "DOUBLE[0.00%]"

    # ---- Level-2 risk decomposition: additive contributions to Scenario VaR 99 ---------------
    # The factor-VaR is the book loss on the tail scenario t* (the 1%-quantile day of the BOOK
    # P&L vector). A member's contribution is its OWN P&L on that SAME book scenario, so the
    # contributions are additive: Σ_member Component = book P&L at t* = Scenario VaR 99.
    #   book_pnl_vec  -> the full-book P&L vector regardless of the current Factor/Security cell
    #                    (tt.total lifts those hierarchies to their top; Date/Book/ScenarioSet
    #                    stay on the current slice, so the tail is the sliced book's tail).
    #   tail_idx      -> index of that book vector's 1% quantile (the VaR scenario).
    book_pnl_vec = tt.total(m["Scenario PnL vector"], h["Security"], h["FactorDim"], h["PositionRank"])
    tail_idx = tt.array.quantile_index(book_pnl_vec, 0.01, interpolation="lower")
    # --- contribution to the FACTOR VaR (Scenario VaR 99): the current cell's P&L at the book's
    #     tail scenario. ADDITIVE: Σ_member = Scenario VaR 99. ("marginal" in the Flex Agg sense.)
    m["Marginal Scenario VaR 99"] = -m["Scenario PnL vector"][tail_idx]
    m["VaR sensitivity"] = m["Marginal Scenario VaR 99"] / m["Net exposure"]   # per-unit ∂VaR/∂exp
    # share of factor VaR: divide by the SUM of the marginals (NOT the interpolated quantile) so
    # it sums to EXACTLY 100%.
    m["% of Scenario VaR 99"] = (m["Marginal Scenario VaR 99"]
        / tt.total(m["Marginal Scenario VaR 99"], h["Security"], h["FactorDim"], h["PositionRank"]))

    # --- contribution to the FACTOR ES 97.5: a member's MEAN P&L over the BOOK's worst-k tail
    #     scenarios (k = ceil(0.025 * n) lowest days of the BOOK P&L vector). Additive like the
    #     VaR marginal (Σ_member = Scenario ES 97.5) but averaged over the whole tail SET rather
    #     than read off the single 1% day -- ES is coherent, so this split is better-behaved.
    #     book_tail_idx = the k lowest INDICES of the book vector; indexing each member's own P&L
    #     vector by that index-array picks its P&L on exactly those book-tail days.
    _k975_book = tt.math.ceil(0.025 * tt.array.len(book_pnl_vec))
    _book_tail_idx = tt.array.n_lowest_indices(book_pnl_vec, _k975_book)
    m["Marginal Scenario ES 97.5"] = -tt.array.mean(m["Scenario PnL vector"][_book_tail_idx])
    m["% of Scenario ES 97.5"] = (m["Marginal Scenario ES 97.5"]
        / tt.total(m["Marginal Scenario ES 97.5"], h["Security"], h["FactorDim"], h["PositionRank"]))

    # --- contribution to TOTAL VaR 99 (factor + specific, combined in quadrature). Euler split of
    #     Total=√(F²+S²): each factor-marginal is scaled by F/Total, and the idiosyncratic block
    #     adds z²·(w²σ²)/Total PER NAME. Σ_member = Total VaR 99.
    #     NB: meaningful only in by-NAME views (Issuer/Sector/Country/Position). By FACTOR the
    #     specific part fans out (specific risk has no factor) -> use Marginal Scenario VaR 99 there.
    F_book = tt.total(m["Scenario VaR 99"], h["Security"], h["FactorDim"], h["PositionRank"])     # book factor VaR
    T_book = tt.total(m["Total VaR 99"], h["Security"], h["FactorDim"], h["PositionRank"])        # book total VaR
    m["Marginal Total VaR 99"] = (m["Marginal Scenario VaR 99"] * F_book / T_book
                                  + (2.326 ** 2) * m["Specific variance"] / T_book)
    m["% of Total VaR 99"] = (m["Marginal Total VaR 99"]
        / tt.total(m["Marginal Total VaR 99"], h["Security"], h["FactorDim"], h["PositionRank"]))

    # ---- concentration: Herfindahl-Hirschman index of risk shares. HHI = Σ_name share², where
    #      share is a NAME's fraction of book Total VaR (its Marginal Total VaR / the book total).
    #      Summed at the POSITION grain via an OriginScope (factors lifted, one term per name),
    #      like Specific variance. Reads 1/N for an evenly-diversified book up to 1.0 for a single
    #      name; the standard single-number concentration gauge a desk watches against a limit.
    _name_share = (m["Marginal Total VaR 99"]
                   / tt.total(m["Marginal Total VaR 99"], h["Security"], h["FactorDim"], h["PositionRank"]))
    m["Risk HHI"] = tt.agg.sum(_name_share * _name_share, scope=tt.OriginScope({l["Position"]}))
    m["Risk HHI"].formatter = "DOUBLE[0.000]"

    # ---- Top-5 risk share: the /limits concentration metric, cube-native via tt.rank ----------
    # tt.rank ranks SIBLINGS, and in the multilevel Security hierarchy a position's siblings are
    # the positions under the same Issuer (~1:1 here — every rank would read 1). The FLAT
    # PositionRank hierarchy (created with the other hierarchies above) gives the global
    # ranking; every book-level tt.total in this file lifts it, so measures evaluated inside a
    # PositionRank context still see the true book totals.
    _tot_r = tt.total(m["Marginal Total VaR 99"], h["Security"], h["FactorDim"], h["PositionRank"])
    _rank_r = tt.rank(m["Marginal Total VaR 99"], h["PositionRank"], ascending=False)
    m["Top-5 risk share"] = tt.agg.sum(
        tt.where(_rank_r <= 5, m["Marginal Total VaR 99"] / _tot_r, 0.0),
        scope=tt.OriginScope({l["PositionR"]}))
    m["Top-5 risk share"].formatter = "DOUBLE[0.0%]"

    # --- INCREMENTAL VaR (Flex Agg sense): REMOVE the current member, recompute the BOOK VaR on
    #     the reduced portfolio, and subtract it from the reference book VaR. Unlike the marginals
    #     this is NOT additive (VaR is sub-additive) — Σ_member Incremental ≤ book VaR — so it
    #     deliberately has no "% of" share. It answers "how much VaR does removing this member
    #     RELEASE", the diversification-aware view a manager uses to decide what to cut.
    #     REFERENCE = the additive book VaR (Σ of the marginals = the tail-scenario READ-OFF), NOT
    #     the interpolated quantile F_book/T_book — so the col-TOTAL row reconciles with the Marginal
    #     column total to the last digit (at the grand level the removed book is empty -> VaR_ex=0,
    #     leaving Incremental == reference == Marginal total). Mixing the two quantile conventions is
    #     exactly what made the totals disagree by ~0.001.
    #       book_var -> reference book factor-VaR = book P&L at the book's own tail scenario.
    #       pnl_ex   -> the book P&L vector with THIS cell's own P&L removed (elementwise array sub).
    #       VaR_ex   -> book factor-VaR recomputed on the reduced vector at ITS OWN tail (removal can
    #                   shift the tail day) — same lower-index read-off convention as the book.
    book_var = -book_pnl_vec[tail_idx]                                   # == tt.total(Marginal Scenario VaR 99)
    pnl_ex = book_pnl_vec - m["Scenario PnL vector"]
    tail_idx_ex = tt.array.quantile_index(pnl_ex, 0.01, interpolation="lower")
    VaR_ex = -pnl_ex[tail_idx_ex]
    m["Incremental Scenario VaR 99"] = book_var - VaR_ex
    # total-VaR analog: strip the member's specific variance too, recombine in quadrature, subtract.
    # Reference = tt.total(Marginal Total VaR 99) (the additive book total) for the same reconciliation.
    book_total = tt.total(m["Marginal Total VaR 99"], h["Security"], h["FactorDim"], h["PositionRank"])
    S_ex_var = tt.total(m["Specific variance"], h["Security"], h["FactorDim"], h["PositionRank"]) - m["Specific variance"]
    Total_ex = tt.math.sqrt(VaR_ex * VaR_ex + (2.326 ** 2) * S_ex_var)
    m["Incremental Total VaR 99"] = book_total - Total_ex

    # ---- Model-vol decomposition: Euler marginal (== CTR) + incremental -----------------------
    # Marginal Model vol: the member's EULER contribution to book sigma — cov(member P&L vector,
    # book P&L vector) plus the member's own specific variance, over book sigma. The covariance
    # comes from the polarization identity cov(a,b) = (var(a+b) − var(a) − var(b))/2 on the SAME
    # sample std Model vol uses, so Σ_member == book Model vol EXACTLY (Euler) and the per-NAME
    # values equal the ch-09 CTR = w·(Σw)/σ that /contributions computes in numpy.
    # Like Marginal Total VaR: meaningful in by-NAME views; by FACTOR the specific block fans out.
    _sigma_book = tt.total(m["Model vol"], h["Security"], h["FactorDim"], h["PositionRank"])
    _cov_book = (tt.array.std(book_pnl_vec + m["Scenario PnL vector"]) ** 2
                 - m["Scenario PnL vol"] ** 2
                 - tt.array.std(book_pnl_vec) ** 2) / 2
    m["Marginal Model vol"] = (_cov_book + m["Specific variance"]) / _sigma_book
    m["% of Model vol"] = (m["Marginal Model vol"]
        / tt.total(m["Marginal Model vol"], h["Security"], h["FactorDim"], h["PositionRank"]))
    # The clean FACTOR-side contribution (no specific block, VARIANCE units): cov(member, book).
    # By Factor: per factor this IS the ch-09 CTV = x_k(Fx)_k (cross-terms 50/50, negative =
    # hedge) and Σ_factor = the factor variance x'Fx. /contributions serves it.
    m["Factor variance contribution"] = _cov_book

    # ---- Tier-1 cube migrations (docs/cube-measure-opportunities.md) -----------------------
    # Factor return vol: std of the RAW factor-return vector (the ShockVec itself, exposure-
    # free) — per Factor on HistFull this is vol_k = std(f_k), the same wide.std() estimator
    # /stress and /reverse_stress used from pandas; on Evt:* sets it reads the window vol.
    # In-cube identity: Scenario PnL vol == |Net exposure| * Factor return vol at any Factor
    # member (std(x·f) = |x|·std(f)).
    _shock_vec = tt.agg.single_value(t_scn["ShockVec"])
    m["Factor return vol"] = tt.array.std(_shock_vec)
    m["Factor return vol"].formatter = "DOUBLE[0.00%]"
    # Vol ex factor: book sigma with the current cell's FACTOR P&L removed but the FULL specific
    # block kept — by FACTOR this is the hedge table's vol-after-neutralizing-k (zeroing x_k
    # cannot touch specific risk; NB Incremental Model vol strips the cell's specific, which is
    # right for NAMES and wrong here — the fan-out trap, handled explicitly).
    _svar_book = tt.total(m["Specific variance"], h["Security"], h["FactorDim"], h["PositionRank"])
    m["Vol ex factor"] = tt.math.sqrt(tt.array.std(pnl_ex) ** 2 + _svar_book)
    m["Vol ex factor"].formatter = "DOUBLE[0.00%]"
    # Min-variance hedge ratio (appendix D6), in EXPOSURE units: h* = −(Fx)_k/F_kk. Via the
    # polarization cov: cov(book, v_k) = x_k·(Fx)_k and var(f_k) = F_kk, so
    # h* = −cov/(x_k·vol_k²) — algebraically x_k cancels out of (Fx)_k, so a near-zero exposure
    # still reads the true ratio (0/0 -> blank only at exactly zero).
    m["Min-variance hedge ratio"] = (-_cov_book
        / (m["Net exposure"] * m["Factor return vol"] ** 2))
    m["Min-variance hedge ratio"].formatter = "DOUBLE[0.000]"
    # Vol at min-variance hedge: book sigma after ADDING h* units of the pure factor-k return
    # stream (specific block untouched) — the D6 single-instrument hedge priced per slice.
    _hedged_vec = book_pnl_vec + m["Min-variance hedge ratio"] * _shock_vec
    m["Vol at min-variance hedge"] = tt.math.sqrt(tt.array.std(_hedged_vec) ** 2 + _svar_book)
    m["Vol at min-variance hedge"].formatter = "DOUBLE[0.00%]"

    # ---- Stressed model vol: the correlation stress as a PARAMETERIZED measure ----------------
    # x'F'x under vols x m and correlations blended b toward 1 has a closed form needing no
    # matrix algebra:  m^2 * [ (1-b) * x'Fx  +  b * (SUM_k x_k*sigma_k)^2 ]   (D J D expansion),
    # and the specific block scales m^2. Same math as _stressed_cov, per CELL — so "this
    # sector's vol when correlations go to 1" is a drill, which the API version can't do.
    # Parameters live on the CorrStress simulation (Base: mult 1, blend 0 -> equals Model vol).
    cube.create_parameter_simulation(
        "CorrStress", measures={"Vol mult": 1.0, "Rho blend": 0.0})
    _sxs = tt.agg.sum(m["Net exposure"] * m["Factor return vol"],
                      scope=tt.OriginScope({l["Factor"]}))          # SUM_k x_k * sigma_k (signed)
    m["Stressed model vol"] = m["Vol mult"] * tt.math.sqrt(
        (1.0 - m["Rho blend"]) * m["Scenario PnL vol"] ** 2
        + m["Rho blend"] * _sxs ** 2
        + m["Specific variance"])
    m["Stressed model vol"].formatter = "DOUBLE[0.00%]"

    # ---- Tier-2 prototype: custom stress as a PARAMETER SIMULATION ----------------------------
    # (docs/cube-measure-opportunities.md #3.) A per-Factor "Shock sigma" parameter, default 0 on
    # the Base scenario. /stress appends a TRANSIENT scenario per request (uuid rows into the
    # "StressShock" table), reads `Custom stress PnL` on that branch, and drops the rows. What
    # the API math cannot do and this gets free: the shock P&L drills by name/sector (each leaf
    # is w·L_k·sigma_k·vol_k, footing exactly), and composes with any cube filter.
    # NB Factor return vol is ScenarioSet-dependent — always read this sliced to HistFull.
    cube.create_parameter_simulation(
        "StressShock", measures={"Shock sigma": 0.0}, levels=[l["Factor"]])
    m["Custom stress PnL"] = tt.agg.sum(
        m["Net exposure"] * m["Shock sigma"] * m["Factor return vol"],
        scope=tt.OriginScope({l["Factor"]}))
    m["Custom stress PnL"].formatter = "DOUBLE[0.00%]"
    # Incremental Model vol: REMOVE the member, recompute sigma on the remainder (its factor
    # vector minus this cell's, its specific variance minus this cell's), subtract from the book
    # sigma. NOT additive (vol is sub-additive) — it answers "how much vol does removing this
    # member release". At the grand level the remainder is empty -> vol_ex = 0, so the total
    # reconciles with the Marginal column (both read the book sigma).
    _vol_ex = tt.math.sqrt(tt.array.std(pnl_ex) ** 2 + S_ex_var)
    m["Incremental Model vol"] = _sigma_book - _vol_ex
    for _mn in ("Marginal Model vol", "Incremental Model vol"):
        m[_mn].formatter = "DOUBLE[0.00%]"

    for _mn in ("Marginal Scenario VaR 99", "Marginal Total VaR 99",
                "Incremental Scenario VaR 99", "Incremental Total VaR 99",
                "Marginal Scenario ES 97.5"):
        m[_mn].formatter = "DOUBLE[0.00%]"
    m["VaR sensitivity"].formatter = "DOUBLE[0.0000]"
    for _mn in ("% of Scenario VaR 99", "% of Total VaR 99", "% of Scenario ES 97.5"):
        m[_mn].formatter = "DOUBLE[0.0%]"

    print(f"cube built: {len(exposures):,} leaf rows, {len(style)} style factors, "
          f"{scenarios['ScenarioSet'].nunique()} scenario sets")
    return session, cube


if __name__ == "__main__":
    session, cube = build_cube(load_frames())
    h, l, m = cube.hierarchies, cube.levels, cube.measures
    last = sorted(cube.query(m["contributors.COUNT"], levels=[l["Date"]]).index)[-1]
    last = pd.Timestamp(last).date()    # Date level stores LocalDate; a datetime filter fails to parse

    # exposures drill (additive)
    print(cube.query(m["Net exposure"], levels=[l["FactorGroup"], l["Factor"]]))

    # THE SWITCH: same measure, every scenario mode, side by side (as-of latest COB)
    print(cube.query(m["Scenario VaR 99"], m["Scenario worst loss"], m["Total VaR 99"],
                     levels=[l["ScenarioSet"]], filter=l["Date"] == last))

    # tail board: VaR ladder + Expected Shortfall + dispersion across every scenario set
    print(cube.query(m["Scenario VaR 95"], m["Scenario VaR 97.5"], m["Scenario VaR 99"],
                     m["Scenario ES 97.5"], m["Scenario ES 99"], m["Scenario PnL vol"],
                     m["Total ES 97.5"], m["Risk HHI"],
                     levels=[l["ScenarioSet"]], filter=l["Date"] == last))

    # factor ES contributions (additive: factors sum to book Scenario ES 97.5)
    print(cube.query(m["Marginal Scenario ES 97.5"], m["% of Scenario ES 97.5"],
                     levels=[l["Factor"]], filter=(l["Date"] == last) & (l["ScenarioSet"] == "HistFull")))

    # any scenario mode still drills the full hierarchy -- e.g. COVID replay by sector
    print(cube.query(m["Scenario worst loss"], levels=[l["Sector"]],
                     filter=(l["Date"] == last) & (l["ScenarioSet"] == "Evt:COVID2020")))

    session.wait()
