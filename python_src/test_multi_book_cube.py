"""
test_multi_book_cube.py — cube-level tests for the Phase 2 multi-manager entity dimension in
barra_factor_risk_cube.py.

Builds a SMALL SYNTHETIC 2-book cube in-process (hand-built pandas frames, no network, no
dependency on the real `data/` parquets or the live :8010 backend) via
barra_factor_risk_cube.build_cube(), on a dedicated local port, and tears it down when done.
Covers the Phase 2 deliverables:

  - the entity dimension (managers.parquet -> Book carries CIK/EntityName/FirmType/ETP-drop
    attributes), queryable and correct per book
  - graceful degradation when managers.parquet is ABSENT (no entity dimension, cube still builds,
    nothing raises) -- the same contract as the pre-existing optional specific_returns frame
  - the PositionRank/Top-5 risk share "landmine": tt.rank must evaluate WITHIN the sliced book,
    not across books. Cross-checked against an independent numpy computation of the same
    quantity, AND against the fact that the two synthetic books carry deliberately different
    weight vectors (a reversed permutation of the same weights) -- if ranking silently ignored
    Book, both books would read some shared/wrong number instead of their own.
  - the tt.total "landmine": the Euler identity (sum(Marginal Model vol) over Position ==
    Model vol) must hold WITHIN a single-book slice, i.e. book-level tt.total calls must stay
    pinned to the currently sliced book rather than lifting across books.

Needs a working local atoti session (repo-root ActivePivot.lic.43457, same requirement as
barra_factor_risk_cube.py itself) -- if the session can't start, SKIP cleanly rather than fail.

Run:  ../barra/bin/python test_multi_book_cube.py
"""
from __future__ import annotations
import os
import pathlib
import traceback

import numpy as np
import pandas as pd

RESULTS = []


def test(fn):
    RESULTS.append(fn)
    return fn


_LICENSE = pathlib.Path(__file__).resolve().parent.parent / "ActivePivot.lic.43457"
if "ATOTI_LICENSE" not in os.environ and _LICENSE.exists():
    os.environ["ATOTI_LICENSE"] = str(_LICENSE)

import barra_factor_risk_cube as C   # noqa: E402  (needs ATOTI_LICENSE set first)

PORT = int(os.environ.get("TEST_CUBE_PORT", "19077"))
DATE = pd.Timestamp("2024-01-31")
POSITIONS = [f"P{i}" for i in range(1, 7)]
FACTORS = ["Market", "Value"]


def _synthetic_frames(with_managers: bool) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(7)

    rows = []
    val_loadings = rng.normal(0, 1, size=len(POSITIONS))
    for pos, vl in zip(POSITIONS, val_loadings):
        rows.append({"Date": DATE, "Position": pos, "Factor": "Market", "Loading": 1.0})
        rows.append({"Date": DATE, "Position": pos, "Factor": "Value", "Loading": float(vl)})
    exposures = pd.DataFrame(rows)

    # 60 trading days of factor-return history (fixed seed, small vol) -> a real, non-degenerate
    # HistFull scenario vector (VaR/ES/Model vol all need >1 point to be meaningful).
    hist_dates = pd.bdate_range("2023-11-01", periods=60)
    fr_rows = []
    for f in FACTORS:
        rets = rng.normal(0, 0.01, size=len(hist_dates))
        for d, r in zip(hist_dates, rets):
            fr_rows.append({"Date": d, "Factor": f, "Return": float(r)})
    factor_returns = pd.DataFrame(fr_rows)

    factor_meta = pd.DataFrame([
        {"Factor": "Market", "FactorGroup": "Market"},
        {"Factor": "Value", "FactorGroup": "Style"},
    ])

    securities = pd.DataFrame([
        {"Position": p, "Ticker": p, "CIK": 1000 + i, "CUSIP": f"CUSIP{i}", "Issuer": f"Issuer {p}",
         "Sector": "Technology" if i % 2 == 0 else "Financials", "Country": "US"}
        for i, p in enumerate(POSITIONS)
    ])

    specific_var = pd.DataFrame([
        {"Date": DATE, "Position": p, "SpecificVar": 0.0004 + 0.0001 * i}
        for i, p in enumerate(POSITIONS)
    ])

    # Two books, SAME universe, DELIBERATELY different weights (BookB = BookA's weight vector
    # reversed across names -- same values, still sums to exactly 1, but assigned to different
    # names) so a within-book-vs-cross-book bug would show up as identical/wrong numbers.
    wa = rng.dirichlet(np.ones(len(POSITIONS)))
    wb = wa[::-1].copy()
    pos_rows = []
    for p, w in zip(POSITIONS, wa):
        pos_rows.append({"Date": DATE, "Book": "BookA", "Position": p, "Weight": float(w),
                          "MV": float(w) * 1e8, "ADV": 5e6})
    for p, w in zip(POSITIONS, wb):
        pos_rows.append({"Date": DATE, "Book": "BookB", "Position": p, "Weight": float(w),
                          "MV": float(w) * 5e7, "ADV": 5e6})
    positions_df = pd.DataFrame(pos_rows)

    # sanity: both should sum to 1 exactly (a permutation of the same values)
    assert abs(positions_df[positions_df["Book"] == "BookA"]["Weight"].sum() - 1.0) < 1e-9
    assert abs(positions_df[positions_df["Book"] == "BookB"]["Weight"].sum() - 1.0) < 1e-9

    frames = {
        "exposures": exposures, "positions": positions_df, "securities": securities,
        "factor_meta": factor_meta, "factor_returns": factor_returns, "specific_var": specific_var,
    }
    if with_managers:
        frames["managers"] = pd.DataFrame([
            {"Book": "BookA", "CIK": 111, "EntityName": "Book A Capital LLC", "FirmType": "hedge_fund",
             "first_filing_date": pd.Timestamp("2020-01-01"), "last_filing_date": DATE,
             "n_filings": 10, "n_distinct_cusips_parsed": 6, "latest_report_date": DATE,
             "n_dropped_rows_all_history": 0, "n_dropped_cusips_all_history": 0, "n_dropped_latest": 0,
             "dropped_value_latest": 0.0, "total_value_latest": 1e8, "dropped_value_share_latest": 0.0,
             "n_positions_distinct": len(POSITIONS)},
            {"Book": "BookB", "CIK": 222, "EntityName": "Book B Partners LP", "FirmType": "family_office",
             "first_filing_date": pd.Timestamp("2019-01-01"), "last_filing_date": DATE,
             "n_filings": 20, "n_distinct_cusips_parsed": 6, "latest_report_date": DATE,
             "n_dropped_rows_all_history": 3, "n_dropped_cusips_all_history": 1, "n_dropped_latest": 0,
             "dropped_value_latest": 0.0, "total_value_latest": 5e7, "dropped_value_share_latest": 0.021,
             "n_positions_distinct": len(POSITIONS)},
        ])
    return frames


# module-level cube handle, set by main() before RESULTS run (mirrors the rest of the suite's
# module-level API/DATE globals, e.g. test_model_vol.py)
CUBE = None


def _q_book(measure: str, book: str):
    h, l, m = CUBE.hierarchies, CUBE.levels, CUBE.measures
    df = CUBE.query(m[measure], filter=(l["Date"] == DATE.date())
                     & (l["ScenarioSet"] == "HistFull") & (l["Book"] == book))
    return float(df.iloc[0, 0])


@test
def t_book_hierarchy_has_two_members():
    h, l, m = CUBE.hierarchies, CUBE.levels, CUBE.measures
    books = sorted(CUBE.query(m["contributors.COUNT"], levels=[l["Book"]]).index)
    assert books == ["BookA", "BookB"], books


@test
def t_entity_attributes_retrieved_per_book():
    h, l, m = CUBE.hierarchies, CUBE.levels, CUBE.measures
    df = CUBE.query(m["Manager n filings"], m["Manager ETP dropped value share"],
                     levels=[l["EntityName"], l["FirmType"], l["CIK"]]).reset_index()
    recs = {r["EntityName"]: r for _, r in df.iterrows()}
    assert set(recs) == {"Book A Capital LLC", "Book B Partners LP"}, recs.keys()
    a, b = recs["Book A Capital LLC"], recs["Book B Partners LP"]
    assert a["FirmType"] == "hedge_fund", a.to_dict()
    assert b["FirmType"] == "family_office", b.to_dict()
    assert int(a["CIK"]) == 111 and int(b["CIK"]) == 222
    assert abs(float(a["Manager ETP dropped value share"]) - 0.0) < 1e-12
    assert abs(float(b["Manager ETP dropped value share"]) - 0.021) < 1e-9


@test
def t_top5_risk_share_matches_independent_numpy_within_book():
    h, l, m = CUBE.hierarchies, CUBE.levels, CUBE.measures
    for book in ("BookA", "BookB"):
        names = CUBE.query(m["Marginal Total VaR 99"], levels=[l["Position"]],
                            filter=(l["Date"] == DATE.date()) & (l["ScenarioSet"] == "HistFull")
                            & (l["Book"] == book))
        names["Marginal Total VaR 99"] = names["Marginal Total VaR 99"].astype(float)
        names = names.sort_values("Marginal Total VaR 99", ascending=False)
        manual = names.head(5)["Marginal Total VaR 99"].sum() / names["Marginal Total VaR 99"].sum()
        cube_val = _q_book("Top-5 risk share", book)
        assert 0 < cube_val <= 1, cube_val
        assert abs(cube_val - manual) < 1e-9, (book, cube_val, manual)


@test
def t_top5_risk_share_differs_between_books():
    """If ranking silently ignored Book (the landmine), BookA and BookB -- which hold the same
    names at deliberately different (reversed) weights -- would coincidentally read the SAME
    number more often than not; assert they don't."""
    a = _q_book("Top-5 risk share", "BookA")
    b = _q_book("Top-5 risk share", "BookB")
    assert a != b, (a, b)


@test
def t_model_vol_and_var_finite_and_book_specific():
    for measure in ("Model vol", "Scenario VaR 99", "Scenario ES 97.5"):
        a = _q_book(measure, "BookA")
        b = _q_book(measure, "BookB")
        assert np.isfinite(a) and np.isfinite(b), (measure, a, b)
        assert a != b, (measure, a, b)


@test
def t_euler_identity_holds_within_book_slice():
    h, l, m = CUBE.hierarchies, CUBE.levels, CUBE.measures
    for book in ("BookA", "BookB"):
        model_vol = _q_book("Model vol", book)
        names = CUBE.query(m["Marginal Model vol"], levels=[l["Position"]],
                            filter=(l["Date"] == DATE.date()) & (l["ScenarioSet"] == "HistFull")
                            & (l["Book"] == book))
        s = float(names["Marginal Model vol"].astype(float).sum())
        assert abs(s - model_vol) < 1e-9, (book, s, model_vol)


def main():
    global CUBE
    p = f = 0
    print("=== multi-book synthetic cube tests (Phase 2 entity dimension) ===")

    try:
        import atoti  # noqa: F401
    except Exception as e:
        print(f"SKIP: atoti not importable ({type(e).__name__}: {e})")
        raise SystemExit(0)

    session = None
    try:
        frames = _synthetic_frames(with_managers=True)
        session, cube = C.build_cube(frames, port=PORT)
        CUBE = cube
    except Exception as e:
        print(f"SKIP: could not build the local test cube ({type(e).__name__}: {e})")
        raise SystemExit(0)

    try:
        for fn in RESULTS:
            try:
                fn()
                print(f"PASS  {fn.__name__}")
                p += 1
            except Exception as e:
                print(f"FAIL  {fn.__name__}: {type(e).__name__}: {e}")
                traceback.print_exc()
                f += 1
    finally:
        session.close()
        CUBE = None

    # graceful degradation: managers.parquet ABSENT -> cube still builds, no entity dimension,
    # nothing else raises. Own short-lived session (a fresh table set, distinct port).
    session2 = None
    try:
        frames2 = _synthetic_frames(with_managers=False)
        assert "managers" not in frames2
        session2, cube2 = C.build_cube(frames2, port=PORT + 1)
        h2, l2, m2 = cube2.hierarchies, cube2.levels, cube2.measures
        assert "Manager" not in {n for _, n in h2}, sorted(n for _, n in h2)
        assert "Manager ETP dropped value share" not in m2
        books = sorted(cube2.query(m2["contributors.COUNT"], levels=[l2["Book"]]).index)
        assert books == ["BookA", "BookB"], books
        df = cube2.query(m2["Model vol"], filter=(l2["Date"] == DATE.date())
                          & (l2["ScenarioSet"] == "HistFull") & (l2["Book"] == "BookA"))
        assert np.isfinite(float(df.iloc[0, 0]))
        print("PASS  t_graceful_degradation_without_managers")
        p += 1
    except Exception as e:
        print(f"FAIL  t_graceful_degradation_without_managers: {type(e).__name__}: {e}")
        traceback.print_exc()
        f += 1
    finally:
        if session2 is not None:
            session2.close()

    print(f"\n{p} passed, {f} failed")
    raise SystemExit(1 if f else 0)


if __name__ == "__main__":
    main()
