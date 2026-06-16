"""
barra_factor_risk_cube.py
=========================
Atoti factor-risk cube (two-block historical model) + unified scenario/stress layer.

Pairs with barra_build_frames.py. Six input frames:
  exposures      (Date, Position, Factor) -> Loading        <-- GRANULAR LEAF
  positions      (Date, Book, Position)   -> Weight, MV      <-- Soros overlay
  securities     (Position)               -> Ticker, CIK, CUSIP, Issuer, Sector, Country
  factor_meta    (Factor)                 -> FactorGroup
  factor_returns (Date, Factor)           -> Return          <-- source of every scenario set
  specific_var   (Date, Position)         -> SpecificVar     <-- diagonal block

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
    return {n: pd.read_parquet(folder / f"{n}.parquet") for n in FRAME_NAMES}


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

    # Precompute the leaf products as PHYSICAL columns. Defining them instead as measure
    # arithmetic (single_value(Loading) x single_value(Weight) under an OriginScope) makes
    # every query re-derive each leaf product in the measure engine — fine on a filtered
    # slice, but a Date x Position x ScenarioSet pivot (~160k cells) took ~9s. As columns,
    # Net exposure / Specific variance become plain SUMs the columnar engine aggregates.
    # Unheld names get Weight 0 and contribute nothing, same as the join's None did.
    w = positions[["Date", "Position", "Weight"]]
    exposures = exposures.merge(w, on=["Date", "Position"], how="left")
    exposures["WLoading"] = exposures["Loading"] * exposures["Weight"].fillna(0.0)
    exposures = exposures.drop(columns="Weight")
    # NB: the same trick must NOT be applied to specific_var — it is a *joined* table, and a
    # plain SUM over a joined column fans out by fact-row multiplicity (each (Date, Position)
    # specvar row is reached once per factor leaf -> ~10x inflated variance, observed √10
    # jump in Specific vol). Its measure keeps the OriginScope form below.

    session = tt.Session.start(tt.SessionConfig(port=port))   # pinned so the UI URL survives restarts
    t_exp = session.read_pandas(exposures,  keys={"Date", "Position", "Factor"}, table_name="Exposures")
    t_pos = session.read_pandas(positions,  keys={"Date", "Book", "Position"},   table_name="Positions")
    t_sec = session.read_pandas(securities, keys={"Position"},                   table_name="Securities")
    t_fm  = session.read_pandas(factor_meta, keys={"Factor"},                    table_name="FactorMeta")
    t_sv  = session.read_pandas(specific,   keys={"Date", "Position"},           table_name="SpecificVar")
    t_scn = session.read_pandas(scenarios,  keys={"ScenarioSet", "Factor"},      table_name="Scenarios")
    t_axis = session.read_pandas(scn_axis,  keys={"ScenarioSet"},                table_name="ScenarioAxis")

    t_exp.join(t_pos, (t_exp["Date"] == t_pos["Date"]) & (t_exp["Position"] == t_pos["Position"]))
    t_exp.join(t_sec, t_exp["Position"] == t_sec["Position"])
    t_exp.join(t_fm,  t_exp["Factor"] == t_fm["Factor"])
    t_exp.join(t_sv,  (t_exp["Date"] == t_sv["Date"]) & (t_exp["Position"] == t_sv["Position"]))
    # PARTIAL join: map Factor only -> ScenarioSet becomes a hierarchy (the "switch"). One ShockVec
    # is selected per (ScenarioSet, Factor); exposures fan across sets but Net exposure is unaffected.
    t_exp.join(t_scn, t_exp["Factor"] == t_scn["Factor"])
    t_scn.join(t_axis, t_scn["ScenarioSet"] == t_axis["ScenarioSet"])   # date axis, one row per set

    cube = session.create_cube(t_exp, mode="manual")
    h, l, m = cube.hierarchies, cube.levels, cube.measures

    h["Security"]  = {"Country": t_sec["Country"], "Sector": t_sec["Sector"],
                      "Issuer": t_sec["Issuer"], "Position": t_exp["Position"]}
    h["FactorDim"] = {"FactorGroup": t_fm["FactorGroup"], "Factor": t_exp["Factor"]}
    h["Date"]      = {"Date": t_exp["Date"]}
    # Book and ScenarioSet are NOT created manually: they are the un-mapped key columns of the
    # partial joins (Positions, Scenarios). In manual cube mode atoti auto-creates their
    # hierarchies once the mapped key columns (Date, Position, Factor) have hierarchies.
    assert {"Book", "ScenarioSet"} <= {n for _, n in h}, sorted(n for _, n in h)

    # ---- additive exposures (drill/slice; independent of ScenarioSet) -------------------------
    # WLoading = Loading x Weight precomputed at load (see top of build_cube): a plain
    # columnar SUM, so deep pivots stay fast.
    m["Net exposure"] = tt.agg.sum(t_exp["WLoading"])
    m["Net exposure"].formatter = "DOUBLE[0.000]"

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
    m["Scenario n"] = tt.array.len(m["Scenario PnL vector"])   # THIS set's vector length
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
    _book_var  = tt.total(m["Scenario VaR 99"], h["Security"], h["FactorDim"])
    _book_loss = tt.total(m["Scenario worst loss"], h["Security"], h["FactorDim"])
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
    # approximate total tail: factor scenario VaR with an independent idiosyncratic tail (z=2.326)
    m["Total VaR 99"] = tt.math.sqrt(m["Scenario VaR 99"] * m["Scenario VaR 99"]
                                     + (2.326 * m["Specific vol"]) ** 2)
    m["Specific vol"].formatter = "DOUBLE[0.00%]"
    m["Total VaR 99"].formatter = "DOUBLE[0.00%]"

    # ---- Level-2 risk decomposition: additive contributions to Scenario VaR 99 ---------------
    # The factor-VaR is the book loss on the tail scenario t* (the 1%-quantile day of the BOOK
    # P&L vector). A member's contribution is its OWN P&L on that SAME book scenario, so the
    # contributions are additive: Σ_member Component = book P&L at t* = Scenario VaR 99.
    #   book_pnl_vec  -> the full-book P&L vector regardless of the current Factor/Security cell
    #                    (tt.total lifts those hierarchies to their top; Date/Book/ScenarioSet
    #                    stay on the current slice, so the tail is the sliced book's tail).
    #   tail_idx      -> index of that book vector's 1% quantile (the VaR scenario).
    book_pnl_vec = tt.total(m["Scenario PnL vector"], h["Security"], h["FactorDim"])
    tail_idx = tt.array.quantile_index(book_pnl_vec, 0.01, interpolation="lower")
    # --- contribution to the FACTOR VaR (Scenario VaR 99): the current cell's P&L at the book's
    #     tail scenario. ADDITIVE: Σ_member = Scenario VaR 99. ("marginal" in the Flex Agg sense.)
    m["Marginal Scenario VaR 99"] = -m["Scenario PnL vector"][tail_idx]
    m["VaR sensitivity"] = m["Marginal Scenario VaR 99"] / m["Net exposure"]   # per-unit ∂VaR/∂exp
    # share of factor VaR: divide by the SUM of the marginals (NOT the interpolated quantile) so
    # it sums to EXACTLY 100%.
    m["% of Scenario VaR 99"] = (m["Marginal Scenario VaR 99"]
        / tt.total(m["Marginal Scenario VaR 99"], h["Security"], h["FactorDim"]))

    # --- contribution to TOTAL VaR 99 (factor + specific, combined in quadrature). Euler split of
    #     Total=√(F²+S²): each factor-marginal is scaled by F/Total, and the idiosyncratic block
    #     adds z²·(w²σ²)/Total PER NAME. Σ_member = Total VaR 99.
    #     NB: meaningful only in by-NAME views (Issuer/Sector/Country/Position). By FACTOR the
    #     specific part fans out (specific risk has no factor) -> use Marginal Scenario VaR 99 there.
    F_book = tt.total(m["Scenario VaR 99"], h["Security"], h["FactorDim"])     # book factor VaR
    T_book = tt.total(m["Total VaR 99"], h["Security"], h["FactorDim"])        # book total VaR
    m["Marginal Total VaR 99"] = (m["Marginal Scenario VaR 99"] * F_book / T_book
                                  + (2.326 ** 2) * m["Specific variance"] / T_book)
    m["% of Total VaR 99"] = (m["Marginal Total VaR 99"]
        / tt.total(m["Marginal Total VaR 99"], h["Security"], h["FactorDim"]))

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
    book_total = tt.total(m["Marginal Total VaR 99"], h["Security"], h["FactorDim"])
    S_ex_var = tt.total(m["Specific variance"], h["Security"], h["FactorDim"]) - m["Specific variance"]
    Total_ex = tt.math.sqrt(VaR_ex * VaR_ex + (2.326 ** 2) * S_ex_var)
    m["Incremental Total VaR 99"] = book_total - Total_ex

    for _mn in ("Marginal Scenario VaR 99", "Marginal Total VaR 99",
                "Incremental Scenario VaR 99", "Incremental Total VaR 99"):
        m[_mn].formatter = "DOUBLE[0.00%]"
    m["VaR sensitivity"].formatter = "DOUBLE[0.0000]"
    for _mn in ("% of Scenario VaR 99", "% of Total VaR 99"):
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

    # any scenario mode still drills the full hierarchy -- e.g. COVID replay by sector
    print(cube.query(m["Scenario worst loss"], levels=[l["Sector"]],
                     filter=(l["Date"] == last) & (l["ScenarioSet"] == "Evt:COVID2020")))

    session.wait()
