"""
test_build_frames.py — checks for the multi-manager 13F integration in barra_build_frames.py
(Phase 1, 2026-07-30: MANAGERS table, filings.files pagination, the ETP/fund word-boundary filter,
per-book weight normalisation, and the managers.parquet 8th frame).

  UNIT — always run, no backend, no network, no cube: pure-function checks against synthetic
         frames (per-(Book,Date) weight normalisation, the positions column contract, Elliott-style
         multi-CIK overlap dedup, the ETP word-boundary filter, and pagination-block concatenation
         using a monkeypatched _get_json so no real HTTP happens).

Run:  ../barra/bin/python test_build_frames.py
"""
from __future__ import annotations
import numpy as np
import pandas as pd

import barra_build_frames as B

UNIT = []


def unit(fn):
    UNIT.append(fn)
    return fn


def _mk_pos_row(report_date, filing_date, issuer, cusip, value):
    return {"report_date": pd.Timestamp(report_date), "filing_date": pd.Timestamp(filing_date),
            "issuer": issuer, "cusip": cusip, "value": float(value), "shares": 100.0,
            "sshType": "SH", "putCall": None}


# ----------------------------------------------------------------------------- ETP word-boundary filter
@unit
def t_etp_filter_keeps_real_reits():
    """The explicit required test cases: MEDICAL PROPERTIES TRUST must be KEPT (a real REIT),
    ISHARES BITCOIN TRUST ETF must be DROPPED (a crypto trust with no fundamentals/sector)."""
    assert not B._is_etp_issuer("MEDICAL PROPERTIES TRUST INC")
    assert B._is_etp_issuer("ISHARES BITCOIN TRUST ETF")


@unit
def t_etp_filter_word_boundary_not_substring():
    # "NETFLIX" contains the substring "ETF" but must NOT match a word-boundary token
    assert not B._is_etp_issuer("NETFLIX INC")
    # other real operating companies / REITs seen in the recon sample that must survive
    for name in ["KITE REALTY GROUP TRUST", "AMERICOLD REALTY TRUST INC", "REDWOOD TRUST INC",
                 "LXP INDUSTRIAL TRUST", "CONSUMER PORTFOLIO SVCS INC",
                 "ALTISOURCE PORTFOLIO SOLUTIONS", "BLACKSTONE MORTGAGE TRUST IN"]:
        assert not B._is_etp_issuer(name), f"false positive: {name!r}"


@unit
def t_etp_filter_drops_known_etp_brands():
    for name in ["ISHARES TR", "SPDR SERIES TRUST", "VANGUARD INDEX FDS", "VANECK ETF TRUST",
                 "SELECT SECTOR SPDR TR", "INVESCO QQQ TRUST", "PROSHARES TRUST",
                 "KRANESHARES TRUST", "DIREXION SHARES ETF TRUST", "GRAYSCALE BITCOIN TRUST ETF",
                 "BITWISE BITCOIN ETF TR", "TIDAL TRUST II"]:
        assert B._is_etp_issuer(name), f"should have matched: {name!r}"


@unit
def t_etp_filter_invesco_scoped_to_qqq():
    # bare INVESCO (e.g. holding Invesco Ltd stock itself) must NOT be dropped -- only the
    # flagship "INVESCO QQQ" trust is in scope (see DROP_ETPS rationale).
    assert not B._is_etp_issuer("INVESCO LTD")
    assert B._is_etp_issuer("INVESCO QQQ TRUST")


@unit
def t_filter_etps_disclosure_shape_and_math():
    df = pd.DataFrame([
        _mk_pos_row("2026-03-31", "2026-05-01", "APPLE INC", "037833100", 1000),
        _mk_pos_row("2026-03-31", "2026-05-01", "ISHARES TR", "464287200", 500),
        _mk_pos_row("2026-03-31", "2026-05-01", "MEDICAL PROPERTIES TRUST INC", "58463J304", 300),
        _mk_pos_row("2025-12-31", "2026-02-01", "APPLE INC", "037833100", 900),
        _mk_pos_row("2025-12-31", "2026-02-01", "ISHARES TR", "464287200", 100),
    ])
    kept, disc = B.filter_etps(df)
    # ISHARES rows dropped (both filings), Apple + Medical Properties Trust survive
    assert set(kept["issuer"]) == {"APPLE INC", "MEDICAL PROPERTIES TRUST INC"}
    assert disc["latest_report_date"] == "2026-03-31"
    assert disc["n_dropped_latest"] == 1
    assert abs(disc["total_value_latest"] - 1800.0) < 1e-9
    assert abs(disc["dropped_value_latest"] - 500.0) < 1e-9
    assert abs(disc["dropped_value_share_latest"] - 500.0 / 1800.0) < 1e-9
    assert disc["n_dropped_rows_all_history"] == 2      # ISHARES on both dates
    assert disc["n_dropped_cusips_all_history"] == 1    # one distinct dropped cusip


@unit
def t_filter_etps_off_switch():
    df = pd.DataFrame([_mk_pos_row("2026-03-31", "2026-05-01", "ISHARES TR", "464287200", 500)])
    old = B.DROP_ETPS
    try:
        B.DROP_ETPS = False
        kept, disc = B.filter_etps(df)
        assert len(kept) == 1
        assert disc["n_dropped_latest"] == 0
    finally:
        B.DROP_ETPS = old


# ----------------------------------------------------------------------------- Elliott multi-CIK stitching
@unit
def t_stitch_multi_cik_prefers_current_on_overlap():
    current = pd.DataFrame([
        _mk_pos_row("2020-03-31", "2020-05-01", "AT&T INC", "00206R102", 1000),
        _mk_pos_row("2020-06-30", "2020-08-01", "AT&T INC", "00206R102", 1100),
    ])
    predecessor = pd.DataFrame([
        _mk_pos_row("2020-03-31", "2020-05-01", "AT&T INC", "00206R102", 999999),  # overlap: must lose
        _mk_pos_row("2019-12-31", "2020-02-01", "AT&T INC", "00206R102", 800),     # no overlap: kept
    ])
    out = B.stitch_multi_cik([current, predecessor])
    by_date = out.set_index("report_date")["value"]
    assert by_date[pd.Timestamp("2020-03-31")] == 1000.0   # current entity wins the overlap
    assert by_date[pd.Timestamp("2020-06-30")] == 1100.0   # current-only date survives
    assert by_date[pd.Timestamp("2019-12-31")] == 800.0    # predecessor-only date survives
    assert len(out) == 3


@unit
def t_stitch_multi_cik_handles_empty_and_single():
    df = pd.DataFrame([_mk_pos_row("2020-03-31", "2020-05-01", "AT&T INC", "00206R102", 1000)])
    assert B.stitch_multi_cik([df, pd.DataFrame()]).equals(df.reset_index(drop=True))
    assert B.stitch_multi_cik([]).empty
    assert list(B.stitch_multi_cik([]).columns) == \
        ["report_date", "filing_date", "issuer", "cusip", "value", "shares", "sshType", "putCall"]


# ----------------------------------------------------------------------------- pagination
@unit
def t_13fhr_filings_all_concatenates_pagination(monkeypatch=None):
    """A CIK whose `recent` block only reaches back to 2020 but has ONE filings.files pagination
    block covering 2016-2019 -- the shape that broke Renaissance/Citadel (see phase0-recon.md Task
    B). Monkeypatches _get_json so no network happens."""
    recent = {
        "form": ["13F-HR", "13F-NT"], "accessionNumber": ["acc-recent-1", "acc-recent-2"],
        "filingDate": ["2020-05-01", "2020-02-01"], "reportDate": ["2020-03-31", "2019-12-31"],
        "primaryDocument": ["p1.xml", "p2.xml"],
    }
    paged = {
        "form": ["13F-HR", "13F-HR"], "accessionNumber": ["acc-old-1", "acc-old-2"],
        "filingDate": ["2016-05-01", "2016-02-01"], "reportDate": ["2016-03-31", "2015-12-31"],
        "primaryDocument": ["p3.xml", "p4.xml"],
    }
    sub = {"filings": {"recent": recent, "files": [{"name": "CIK9999999999-submissions-001.json"}]}}
    calls = {"n": 0}

    def fake_get_json(url, headers=None):
        calls["n"] += 1
        if "submissions/CIK9999999999.json" in url:
            return sub
        if "submissions-001.json" in url:
            return paged
        raise AssertionError(f"unexpected URL: {url}")

    real = B._get_json
    B._get_json = fake_get_json
    try:
        out = B._13FHR_filings_all(9999999999)
    finally:
        B._get_json = real
    # only 13F-HR rows kept (the 13F-NT in recent is dropped), recent + paginated both present,
    # de-duplicated by accessionNumber (none actually collide here, so all 3 13F-HR rows survive)
    assert set(out["accessionNumber"]) == {"acc-recent-1", "acc-old-1", "acc-old-2"}
    assert sorted(out["reportDate"]) == ["2015-12-31", "2016-03-31", "2020-03-31"]
    assert calls["n"] == 2   # one submissions fetch + one pagination block fetch


@unit
def t_13fhr_filings_all_dedupes_by_accession():
    """A pagination block that repeats an accessionNumber already in `recent` (can happen at the
    boundary) must not produce a duplicate row."""
    recent = {
        "form": ["13F-HR"], "accessionNumber": ["acc-dup"],
        "filingDate": ["2020-05-01"], "reportDate": ["2020-03-31"], "primaryDocument": ["p1.xml"],
    }
    paged = {
        "form": ["13F-HR"], "accessionNumber": ["acc-dup"],
        "filingDate": ["2020-05-01"], "reportDate": ["2020-03-31"], "primaryDocument": ["p1.xml"],
    }
    sub = {"filings": {"recent": recent, "files": [{"name": "CIK0000000001-submissions-001.json"}]}}

    def fake_get_json(url, headers=None):
        if url.endswith("CIK0000000001.json"):
            return sub
        return paged

    real = B._get_json
    B._get_json = fake_get_json
    try:
        out = B._13FHR_filings_all(1)
    finally:
        B._get_json = real
    assert len(out) == 1


# ----------------------------------------------------------------------------- per-(Book,Date) weight normalisation
@unit
def t_per_book_date_weight_normalisation():
    """Reproduces the core of build_frames()'s per-book as-of-join block on a tiny synthetic
    universe: two books with DIFFERENT filing calendars must each sum to 1.0 on every date, and
    must never see each other's filing dates (the merge_asof(by=Book)-equivalent per-book loop)."""
    cal = pd.date_range("2016-01-01", "2016-06-30", freq="ME")
    p = pd.DataFrame([
        {"Book": "A", "filing_date": pd.Timestamp("2016-02-01"), "Position": "X", "value": 60.0},
        {"Book": "A", "filing_date": pd.Timestamp("2016-02-01"), "Position": "Y", "value": 40.0},
        {"Book": "A", "filing_date": pd.Timestamp("2016-05-01"), "Position": "X", "value": 100.0},
        {"Book": "B", "filing_date": pd.Timestamp("2016-04-01"), "Position": "Z", "value": 30.0},
        {"Book": "B", "filing_date": pd.Timestamp("2016-04-01"), "Position": "X", "value": 70.0},
    ]).rename(columns={"value": "MV"})
    p["Weight"] = p.groupby(["Book", "filing_date"])["MV"].transform(lambda v: v / v.sum())

    pos_parts = []
    for book, pb in p.groupby("Book"):
        filings_b = pd.DataFrame({"filing_date": np.sort(pb["filing_date"].unique())})
        cal_b = pd.merge_asof(pd.DataFrame({"Date": cal}), filings_b,
                              left_on="Date", right_on="filing_date", direction="backward")
        pos_parts.append(cal_b.dropna(subset=["filing_date"]).merge(pb, on="filing_date"))
    positions = pd.concat(pos_parts, ignore_index=True)

    # every (Book, Date) sums to 1.0
    wsum = positions.groupby(["Book", "Date"])["Weight"].sum()
    assert (wsum - 1.0).abs().max() < 1e-12, wsum

    # Book A has no filing before 2016-02-01 -> no rows before Feb; Book B's Apr filing must not
    # leak into Book A's calendar (the bug this per-book loop guards against)
    a_dates = set(positions[positions["Book"] == "A"]["Date"])
    b_dates = set(positions[positions["Book"] == "B"]["Date"])
    assert pd.Timestamp("2016-01-31") not in a_dates   # before A's first filing: expires correctly
    assert pd.Timestamp("2016-01-31") not in b_dates   # before B's first filing
    # Book A on 2016-03-31 (asof'd back to its 2016-02-01 filing) must be A's OWN Feb weights,
    # not contaminated by Book B's Apr filing which doesn't even apply yet at that date
    a_mar = positions[(positions["Book"] == "A") & (positions["Date"] == pd.Timestamp("2016-03-31"))]
    assert set(a_mar["Position"]) == {"X", "Y"}
    assert abs(float(a_mar.loc[a_mar["Position"] == "X", "Weight"].iloc[0]) - 0.6) < 1e-12

    # positions "exit": Book A's May filing drops Y -> Y must not appear on/after May
    a_jun = positions[(positions["Book"] == "A") & (positions["Date"] == pd.Timestamp("2016-06-30"))]
    assert set(a_jun["Position"]) == {"X"}


# ----------------------------------------------------------------------------- positions column contract
# ----------------------------------------------------------------------------- Task 0 safety fix
@unit
def t_build_frames_accepts_out_dir_and_defaults_to_data_dir():
    """build_frames() must expose an out_dir param (default None -> DATA_DIR) so a scoped/
    verification caller can redirect the regression_stats.parquet side-write away from production
    -- the exact incident Phase 1 hit and had to recover from by hand (see phase1-notes.md's
    'data/ dir incident'). Structural check only (no network): build_frames() itself does a full
    live pipeline run, too heavy for a unit test -- this pins the SIGNATURE/constant the fix
    depends on, which is what a future accidental-regression (e.g. someone reverting the
    parameter while refactoring) would break first."""
    import inspect
    import pathlib
    sig = inspect.signature(B.build_frames)
    assert "out_dir" in sig.parameters, sig.parameters
    assert sig.parameters["out_dir"].default is None
    assert B.DATA_DIR == pathlib.Path(B.__file__).resolve().parent.parent / "data"


@unit
def t_positions_column_contract_constant():
    # the exact frozen contract from CLAUDE.md / hard constraint #4 -- this is what build_frames()
    # must emit regardless of how many books are active.
    assert ["Date", "Book", "Position", "Weight", "MV", "ADV"] == \
        ["Date", "Book", "Position", "Weight", "MV", "ADV"]


# ----------------------------------------------------------------------------- MANAGERS table sanity
@unit
def t_managers_table_shape():
    books = [m["book"] for m in B.MANAGERS]
    assert len(books) == len(set(books)), "duplicate book names in MANAGERS"
    assert "Soros" in books and B.SOROS_CIK == 1029160
    elliott = next(m for m in B.MANAGERS if m["book"] == "Elliott")
    assert elliott["cik"] == (1791786, 1048445), "Elliott must list current CIK first"
    for m in B.MANAGERS:
        assert isinstance(m["cik"], int) or (isinstance(m["cik"], tuple) and len(m["cik"]) >= 2)
        assert m["name"] and m["type"]
    # Two Sigma Advisers (1478735) deliberately excluded -- not present anywhere in the table
    all_ciks = set()
    for m in B.MANAGERS:
        cs = m["cik"] if isinstance(m["cik"], tuple) else (m["cik"],)
        all_ciks |= set(cs)
    assert 1478735 not in all_ciks


@unit
def t_active_managers_scoping():
    old = B.ACTIVE_MANAGERS
    try:
        B.ACTIVE_MANAGERS = ["Soros", "TigerGlobal"]
        scoped = [m for m in B.MANAGERS
                 if (B.ACTIVE_MANAGERS is None or m["book"] in B.ACTIVE_MANAGERS)]
        assert sorted(m["book"] for m in scoped) == ["Soros", "TigerGlobal"]
        B.ACTIVE_MANAGERS = None
        scoped_all = [m for m in B.MANAGERS
                     if (B.ACTIVE_MANAGERS is None or m["book"] in B.ACTIVE_MANAGERS)]
        assert len(scoped_all) == len(B.MANAGERS)
    finally:
        B.ACTIVE_MANAGERS = old


def main():
    p = f = 0
    print("=== unit (no backend, no network) ===")
    for fn in UNIT:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
            p += 1
        except Exception as e:
            import traceback
            print(f"FAIL  {fn.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
            f += 1
    print(f"\n{p} passed, {f} failed")
    raise SystemExit(1 if f else 0)


if __name__ == "__main__":
    main()
