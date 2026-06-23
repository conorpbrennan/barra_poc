"""
barra_universe_membership.py
============================
Phase 1 of the universe diagnostics (see docs/universe-diagnostics-plan.md): a BITEMPORAL
index-membership read of the Soros 13F book. For each 13F filing it classifies every held name
by which index it sat in, weight-aggregates into mutually-exclusive buckets, and writes
data/universe_membership.parquet for the /universe endpoint and the dashboard panel.

Bitemporal:
  * report_date  = VALID time      — the quarter Soros actually held the name.
  * filing_date  = KNOWLEDGE time  — when the holding became public (13F lands <=45 days after
    quarter-end). Carried on every row.
S&P 500 membership is read AS-OF report_date from the constituent list as it stood THEN
(point-in-time, survivorship-bias-free), not from today's list.

Two tracks, set by what free data actually supports (see the plan's feasibility table):
  Track A  S&P 500   — TRUE point-in-time, from the hanshof historical change log (effective dates).
  Track B  S&P 1500  — CURRENT membership only (Wikipedia 500/400/600 lists), flagged not-PIT.
  Russell 3000       — NOT classified: no free source (iShares serves HTML, FTSE/Norgate are paid).
                       The "Outside S&P 1500" bucket is the coverage-beyond-estimation set anyway.

Buckets (priority order), aggregated BY 13F WEIGHT, not name count:
  1. "S&P 500"            in S&P 500 at report_date (PIT, Track A) — the clean estimation core.
  2. "S&P 400/600"        not PIT-S&P 500 but in today's S&P 1500 (Track B) — rest of the 1500.
  3. "Outside S&P 1500"   in neither — the names that force coverage beyond an SP1500 estimation set.

Pure logic (parse / as-of lookup / bucketing / aggregation) is split out so it is unit-testable
without the network. The fetchers reuse barra_build_frames's disk cache, so this is a precompute
step (like the builder); the /universe endpoint then only reads the parquet — no network at request
time, no cube dependency.

CLI:  cd python_src && ../barra/bin/python barra_universe_membership.py
"""
from __future__ import annotations
import io
import pathlib

import pandas as pd

from barra_build_frames import (_get, positions_from_13f, crosswalk_cusips, ticker_to_cik,
                                SOROS_CIK)

OUT = pathlib.Path(__file__).resolve().parent.parent / "data"
ARTIFACT = OUT / "universe_membership.parquet"

HANSHOF_URL = ("https://raw.githubusercontent.com/hanshof/sp500_constituents/"
               "main/sp_500_historical_components.csv")
WIKI = {"sp500": "List_of_S%26P_500_companies",
        "sp400": "List_of_S%26P_400_companies",
        "sp600": "List_of_S%26P_600_companies"}
_UA = {"User-Agent": "Mozilla/5.0 (universe-diagnostic)"}

BUCKETS = ["S&P 500", "S&P 400/600", "Outside S&P 1500", "Unclassified"]
NOTES = {
    "sp500": "S&P 500 — point-in-time (as-of the filing's report date), survivorship-bias-free.",
    "sp1500": "S&P 1500 (S&P 500 ∪ 400 ∪ 600) — CURRENT membership applied to all dates, NOT "
              "point-in-time. Today's lists can't contain since-delisted names. Membership is decided "
              "by ticker OR company name, so foreign-domicile names (Linde, Accenture) resolve even "
              "when their CUSIP has no US ticker.",
    "russell": "Russell 3000 — not classified: no free source (iShares serves HTML, FTSE is paid). "
               "'Outside S&P 1500' is the coverage-beyond-estimation set regardless.",
    "ticker": "Names are matched on current ticker (CUSIP→OpenFIGI) and on normalized issuer name; "
              "historical ticker drift is a known POC caveat.",
    "unclassified": "Unclassified — no US ticker AND no S&P 1500 name match (since-delisted/acquired "
                    "names, foreign-only listings, odd CUSIP variants). Held OUT of the 'outside S&P "
                    "1500' headline, not folded in. Coverage is strong on recent filings and thins on "
                    "older ones as accumulated delisted names lose their US ticker — read the latest "
                    "filing's headline as the reliable one and treat the long history as context.",
}

# issuer-name suffixes stripped before matching, so '13F "LINDE PLC"' meets Wikipedia '"Linde plc"'.
_NAME_SUFFIX = {"INC", "CORP", "CORPORATION", "CO", "COMPANY", "PLC", "LTD", "LIMITED", "LLC", "LP",
                "SA", "NV", "AG", "THE", "NEW", "DEL", "HLDGS", "HOLDING", "HOLDINGS", "GROUP",
                "CL", "CLASS", "COM", "ADR", "REIT", "TR", "TRUST"}


# --------------------------------------------------------------------------- pure logic (unit-tested)
def canon(t) -> str:
    """Canonical ticker key: upper-case, class separators unified ('.'/' ' -> '-'). So the
    Stooq-style 'brk-b', Wikipedia 'BRK.B' and the change-log all collapse to 'BRK-B'."""
    if t is None:
        return ""
    s = str(t).strip().upper().replace(".", "-").replace(" ", "-")
    return s


def parse_sp500_history(raw: bytes) -> list[tuple[pd.Timestamp, frozenset]]:
    """Parse the hanshof 'date,tickers' change log into (effective_date, {canon tickers}) snapshots,
    sorted ascending. Each row is the full constituent list as of that date."""
    df = pd.read_csv(io.BytesIO(raw))
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    out = []
    for _, r in df.iterrows():
        toks = str(r["tickers"]).split(",")
        out.append((pd.Timestamp(r["date"]), frozenset(canon(t) for t in toks if str(t).strip())))
    return out


def sp500_member_asof(history: list[tuple[pd.Timestamp, frozenset]], ticker: str,
                      date: pd.Timestamp) -> bool:
    """Was `ticker` in the S&P 500 as of `date`? Uses the latest snapshot whose effective date is
    <= date (point-in-time). Returns False before the first snapshot."""
    tk = canon(ticker)
    if not tk:
        return False
    snap = None
    for d, members in history:                      # history is sorted ascending
        if d <= date:
            snap = members
        else:
            break
    return bool(snap and tk in snap)


def norm_name(s) -> str:
    """Normalize an issuer/company name for cross-source matching: upper-case, punctuation to space,
    drop corporate-form suffix tokens, collapse whitespace. 'LINDE PLC' and 'Linde plc' -> 'LINDE'."""
    import re
    s = re.sub(r"[^A-Z0-9 ]", " ", str(s).upper())
    toks = [t for t in s.split() if t and t not in _NAME_SUFFIX]
    return " ".join(toks)


def bucket_of(resolved: bool, in_sp500_pit: bool, in_sp1500_now: bool) -> str:
    """Mutually-exclusive bucket. Unresolved names (no ticker, no name match) are 'Unclassified',
    NOT 'Outside S&P 1500' — so the headline can't be inflated by identity-resolution gaps. Priority
    among resolved names: PIT S&P 500 > current S&P 1500 > outside."""
    if not resolved:
        return "Unclassified"
    if in_sp500_pit:
        return "S&P 500"
    if in_sp1500_now:
        return "S&P 400/600"
    return "Outside S&P 1500"


def aggregate(detail: pd.DataFrame) -> pd.DataFrame:
    """Name-level detail -> per-(report_date) weight + name-count by bucket (long form)."""
    g = (detail.groupby(["report_date", "bucket"], as_index=False)
         .agg(weight=("weight", "sum"), n_names=("weight", "size")))
    return g.sort_values(["report_date", "bucket"]).reset_index(drop=True)


# --------------------------------------------------------------------------- fetchers (cached)
def load_sp500_history() -> list[tuple[pd.Timestamp, frozenset]]:
    return parse_sp500_history(_get(HANSHOF_URL, headers=_UA))


def _wiki_constituents(page: str) -> list[tuple[str, str]]:
    """Current (Symbol, Security-name) pairs from a Wikipedia 'List of S&P NNN companies' table,
    parsed with bs4's built-in html parser (no lxml/html5lib dependency)."""
    from bs4 import BeautifulSoup
    html = _get(f"https://en.wikipedia.org/wiki/{page}", headers=_UA).decode("utf-8", "replace")
    soup = BeautifulSoup(html, "html.parser")
    for tbl in soup.find_all("table", class_="wikitable"):
        heads = [th.get_text(strip=True) for th in tbl.find_all("th")]
        sym_c = next((i for i, h in enumerate(heads) if h in ("Symbol", "Ticker")), None)
        nm_c = next((i for i, h in enumerate(heads) if h in ("Security", "Company", "Name")), None)
        if sym_c is None:
            continue
        rows = []
        for tr in tbl.find_all("tr")[1:]:
            cells = tr.find_all(["td", "th"])
            if len(cells) > sym_c:
                sym = cells[sym_c].get_text(strip=True)
                nm = cells[nm_c].get_text(strip=True) if (nm_c is not None and len(cells) > nm_c) else ""
                if sym:
                    rows.append((sym, nm))
        if len(rows) > 200:                          # the constituents table, not a small side table
            return rows
    return []


def load_sp1500_now() -> tuple[frozenset, frozenset, dict]:
    """Current S&P 1500 = S&P 500 ∪ 400 ∪ 600. Returns (canon-ticker set, normalized-name set,
    {normalized-name: canon-ticker}). The name set/map let foreign-domicile holdings whose CUSIP has
    no US ticker still resolve and classify."""
    tickers: set[str] = set()
    names: set[str] = set()
    name2sym: dict[str, str] = {}
    for key in ("sp500", "sp400", "sp600"):
        for sym, nm in _wiki_constituents(WIKI[key]):
            c = canon(sym)
            tickers.add(c)
            k = norm_name(nm)
            if k:
                names.add(k)
                name2sym.setdefault(k, c)
    return frozenset(tickers), frozenset(names), name2sym


def load_sec_namemap() -> dict[str, str]:
    """{normalized issuer name: canon ticker} from SEC company_tickers.json (~10k US filers).
    Authoritative name->ticker recovery for held names whose CUSIP didn't crosswalk (e.g. SEALED
    AIR CORP NEW -> SEE), so they classify instead of falling to 'Unclassified'."""
    t2c = ticker_to_cik()                               # columns: ticker, cik, title
    m: dict[str, str] = {}
    for t, title in zip(t2c["ticker"], t2c["title"]):
        k = norm_name(title)
        if k:
            m.setdefault(k, canon(t))
    return m


def held_ticker_map(cusips: list[str]) -> dict[str, str]:
    """CUSIP -> canon ticker for every held name. Primary source is OpenFIGI via the builder's
    own `crosswalk_cusips` — called with the FULL held list so the batch payloads match the ones the
    builder already cached (hits disk, no live OpenFIGI calls / 429s). The built securities frame
    fills any CUSIP OpenFIGI returned blank."""
    m: dict[str, str] = {}
    cw = crosswalk_cusips(list(cusips))                 # same call/batching as the builder -> warm cache
    for _, r in cw.iterrows():
        if r.get("ticker"):
            m[str(r["cusip"]).upper()] = canon(r["ticker"])
    sec_path = OUT / "securities.parquet"
    if sec_path.exists():                               # fallback for OpenFIGI blanks
        sec = pd.read_parquet(sec_path)[["CUSIP", "Ticker"]].dropna()
        for c, t in zip(sec["CUSIP"], sec["Ticker"]):
            cu = str(c).upper()
            if cu not in m and str(t).strip():
                m[cu] = canon(t)
    return m


# --------------------------------------------------------------------------- build
def build(holdings: pd.DataFrame, tmap: dict[str, str], sp500_hist: list,
          sp1500_tickers: frozenset, sp1500_names: frozenset, name2sym: dict,
          sec_namemap: dict) -> pd.DataFrame:
    """Name-level membership detail for every (latest-filing-per-quarter) holding.

    Identity per name (in order): ticker from the CUSIP crosswalk, else from the SEC name map
    (company_tickers.json), else from the current-S&P-1500 name map. Current-S&P-1500 membership is
    decided by ticker OR normalized name. A name with neither a ticker nor a name match is
    'Unclassified', kept out of the 'outside' headline."""
    h = holdings.copy()
    h["report_date"] = pd.to_datetime(h["report_date"])
    h["filing_date"] = pd.to_datetime(h["filing_date"])
    # one filing per quarter: keep the latest filing_date per report_date (drops any amendments)
    latest = h.groupby("report_date")["filing_date"].transform("max")
    h = h[h["filing_date"] == latest].copy()
    # weights within each filing (value is scale-free; sums to 1.0 per report_date)
    h["weight"] = h["value"] / h.groupby("report_date")["value"].transform("sum")

    rows = []
    for _, r in h.iterrows():
        nm = norm_name(r["issuer"])
        ticker = (tmap.get(str(r["cusip"]).upper(), "")
                  or sec_namemap.get(nm, "") or name2sym.get(nm, ""))
        in_now = (bool(ticker) and canon(ticker) in sp1500_tickers) or (nm in sp1500_names)
        in_pit = sp500_member_asof(sp500_hist, ticker, r["report_date"]) if ticker else False
        resolved = bool(ticker) or in_now
        rows.append({
            "report_date": r["report_date"], "filing_date": r["filing_date"],
            "cusip": str(r["cusip"]).upper(), "ticker": ticker, "issuer": r["issuer"],
            "weight": float(r["weight"]), "in_sp500_pit": bool(in_pit), "in_sp1500_now": bool(in_now),
            "bucket": bucket_of(resolved, in_pit, in_now),
        })
    return pd.DataFrame(rows).sort_values(["report_date", "weight"], ascending=[True, False])


def run(write: bool = True) -> dict:
    print("[universe] parsing 13F holdings ...", flush=True)
    holdings = positions_from_13f(SOROS_CIK)
    cusips = sorted({str(c).upper() for c in holdings["cusip"].dropna()})
    print(f"[universe] {len(holdings)} holding-rows, {len(cusips)} unique CUSIPs", flush=True)

    print("[universe] mapping CUSIP->ticker + SEC name map ...", flush=True)
    tmap = held_ticker_map(cusips)
    sec_namemap = load_sec_namemap()
    print("[universe] loading S&P 500 PIT history (hanshof) ...", flush=True)
    sp500_hist = load_sp500_history()
    print("[universe] loading current S&P 1500 (Wikipedia 500/400/600) ...", flush=True)
    sp1500_tickers, sp1500_names, name2sym = load_sp1500_now()
    print(f"[universe] S&P 500 snapshots={len(sp500_hist)}  current S&P 1500="
          f"{len(sp1500_tickers)} tickers / {len(sp1500_names)} names", flush=True)

    detail = build(holdings, tmap, sp500_hist, sp1500_tickers, sp1500_names, name2sym, sec_namemap)
    series = aggregate(detail)

    if write:
        OUT.mkdir(parents=True, exist_ok=True)
        detail.to_parquet(ARTIFACT, index=False)
        print(f"[universe] wrote {ARTIFACT}  ({len(detail)} rows)", flush=True)

    # quick PASS/print summary on the latest filing
    last = detail["report_date"].max()
    latest = detail[detail["report_date"] == last]
    out_w = latest.loc[latest["bucket"] == "Outside S&P 1500", "weight"].sum()
    unc_w = latest.loc[latest["bucket"] == "Unclassified", "weight"].sum()
    print(f"\n[universe] latest filing {last.date()}  ({len(latest)} names)")
    for b in BUCKETS:
        w = latest.loc[latest["bucket"] == b, "weight"].sum()
        n = int((latest["bucket"] == b).sum())
        print(f"    {b:18s}  {w:6.1%}   {n:3d} names")
    print(f"\n[universe] HEADLINE: {out_w:.1%} of latest book weight sits OUTSIDE S&P 1500 "
          f"({unc_w:.1%} unclassified, held out of the headline)")
    return {"detail": detail, "series": series, "headline_outside_sp1500": float(out_w),
            "unclassified": float(unc_w), "latest_date": str(last.date())}


if __name__ == "__main__":
    run()
