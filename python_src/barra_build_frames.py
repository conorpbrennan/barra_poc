"""
barra_build_frames.py
=====================
Real, all-open data pipeline that produces the frames consumed by the Atoti factor-risk cube
(barra_factor_risk_cube.py). Everything is free / public-domain:

    positions     -> SEC EDGAR 13F (multi-manager -- see MANAGERS; Soros Fund Management, CIK
                     0001029160, was the original single-book baseline)
    fundamentals  -> SEC EDGAR XBRL company-facts API  (point-in-time, CIK-keyed)
    prices/returns-> Stooq per-symbol daily CSV         (ticker-keyed)
    crosswalk     -> OpenFIGI v3 mapping  (CUSIP -> FIGI/ticker)  +  SEC company_tickers.json

Design (settled with the desk):
  * Leaf = full estimation-universe exposure panel  (Date, Position, Factor) -> Loading.
  * Each manager's 13F is a WEIGHT OVERLAY on that leaf, as-of joined PER BOOK (quarterly, lagged
    by filing date) -- see MANAGERS / build_frames for the multi-manager integration (Phase 1,
    2026-07-30).
  * Two blocks only: linear factor P&L (factor-return cache) + diagonal specific risk.
  * factor_returns and specific_var are DERIVED from a DAILY cross-sectional regression on
    monthly-updated exposures, not downloaded -- so exposures, factor returns and specific
    risk are duals of one regression, and all scenario VaR numbers are 1-day horizon.

Emits seven frames (the cube contract) plus an optional eighth (managers, dimension-like, not part
of the contract -- see Task E notes on MANAGERS):
  exposures        (Date, Position, Factor) -> Loading      <-- GRANULAR LEAF
  positions        (Date, Book, Position)   -> Weight, MV, ADV <-- per-manager overlay (ADV = days-to-liquidate)
  securities       (Position)               -> Ticker, CIK, CUSIP, Issuer, Sector, Country
  factor_meta      (Factor)                 -> FactorGroup
  factor_returns   (Date, Factor)           -> Return        <-- the shared cache
  specific_var     (Date, Position)         -> SpecificVar   <-- diagonal block
  specific_returns (Date, Position)         -> SpecificReturn <-- daily WLS residual u (PnL attribution)
  managers         (Book)                   -> CIK, EntityName, FirmType, filing/coverage stats, ETP
                                                drop disclosure  <-- OPTIONAL 8th frame, dimension-like

NB Position == canonical SecId == FIGI (resolved up front so the cube only joins on SecId).

PRODUCTION CAVEATS (flagged, not hidden):
  - Set SEC_UA to a real "name email" string; SEC blocks anonymous traffic.
  - Stooq has a daily request quota and Yahoo/Stooq are free-not-open-licensed (noted earlier).
  - Universe is capped by default so it actually runs; widen UNIVERSE_EXTRA for a real model
    (factor-return quality scales with cross-section breadth).
  - HTTP responses are disk-cached under CACHE_DIR so reruns are cheap and polite.
"""
from __future__ import annotations
import io, json, os, re, time, pathlib, hashlib, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import numpy as np
import pandas as pd
import requests

# --------------------------------------------------------------------------- config
SEC_UA          = "Conor Brennan conorpbrennan@gmail.com"   # <-- REQUIRED: put a real contact here
SOROS_CIK       = 1029160          # kept: barra_build_frames_v1.py and other code import this
                                    # constant directly (Soros-only, single-CIK). See MANAGERS below
                                    # for the multi-manager 13F integration (Phase 1, 2026-07-30).
OPENFIGI_KEY    = os.environ.get("OPENFIGI_KEY") or None   # optional; raises batch/rate limits if set
CACHE_DIR       = pathlib.Path(__file__).resolve().parent.parent / "tmp"   # repo-local HTTP cache
DATA_DIR        = pathlib.Path(__file__).resolve().parent.parent / "data"  # production frame output
# ^ the ONE default output dir every frame-writer (the __main__ block below, and build_frames'
# own regression_stats.parquet side-write) resolves against unless a caller overrides it via
# build_frames(out_dir=...). Keeping both writers pointed at this single constant is what makes an
# override actually redirect ALL outputs, not just the seven frames __main__ writes explicitly.
START, END      = "2016-01-01", "2026-06-30"   # END = last complete month-end (extended 2026-07-04)
EWMA_HALFLIFE_D = 63              # trading days, for the specific-variance EWMA (matches v1)
MCAP_FLOOR      = 1e7             # $10M; below this, shares data is assumed corrupt (see build_exposures)
UNIVERSE_CAP    = 35000           # safety bound only; the universe is now index-seeded (see SEED_INDEX).
                                  # Raised 2026-07-30 (multi-manager Phase 1): sec.head(UNIVERSE_CAP) is
                                  # ORDER-DEPENDENT and silently truncates -- the measured union of held
                                  # CUSIPs across the 11-manager MANAGERS table is ~22,113 (phase0b recon),
                                  # so the old 3500 cap would have silently dropped the majority of names.
                                  # 35000 leaves headroom above the measured union.
UNIVERSE_EXTRA  = []             # add tickers (e.g. an index list) to broaden the estimation universe
SEED_INDEX      = "sp500"         # seed the estimation cross-section with a market index (None to disable)
UNCAP_COVERAGE  = True            # estimation/coverage split (Chris's DQ point): standardize on the
                                  # estimation universe (the index seed), winsorize estimation loadings,
                                  # leave COVERAGE (held-but-not-estimation) loadings UNCAPPED, and fit
                                  # factor returns on estimation names only. False -> legacy (cap all).
COVERAGE_CAP    = 10.0            # backstop clip for coverage loadings: lets a genuine large tilt (a
                                  # tiny name's Size ~ -6) through, but clamps corrupt-data blowups
                                  # (negative-equity Leverage, etc.). Estimation stays winsorized at ±3.
SP500_URL       = ("https://raw.githubusercontent.com/datasets/"
                   "s-and-p-500-companies/main/data/constituents.csv")
MARKET_PROXY    = "spy"           # Stooq symbol for the market factor / beta regression
RATE_PROXY      = "tlt"           # 20y+ Treasury ETF: the RateBeta (duration) descriptor's proxy
NDX_PROXY       = "qqq"           # Nasdaq-100 ETF: the NdxBeta (mega-complex comovement) proxy

# --------------------------------------------------------------------------- multi-manager 13F (Phase 1)
# MANAGERS: one row per BOOK. "cik" is a single int, or a tuple of ints for a manager that renamed/
# re-filed under a new legal entity (the FIRST cik in the tuple is the CURRENT entity and wins on any
# report_date both cover -- see stitch_multi_cik). CIKs verified by phase0-recon (exact-name EDGAR
# entity match + rejected-alternates check, see scratchpad/phase0-recon.md); measured per-manager
# scale by phase0b (2026-07-30, positions_from_13f on `filings.recent` only, i.e. BEFORE the Task B
# pagination fix below -- Renaissance/Citadel's true history is deeper than these counts show):
#
#   book          filings  cusips_all  latest_cusips  latest_value   etf_wb  (recent-only, pre-pagination)
#   Soros              52       2,461            229    $5.53bn          10
#   Bridgewater        52       2,293            993    $22.40bn         13
#   Citadel            41      17,921          5,960    $138.7bn       1,100
#   Millennium         52      13,668          3,735    $127.7bn         111
#   Renaissance        26       8,619          3,213    $63.93bn         199
#   AQR                52       7,420          3,739    $218.4bn         380
#   TwoSigma           52       9,602          3,593    $117.1bn         155
#   DEShaw             52      10,084          3,102    $119.0bn         128
#   Point72            46       5,707          1,904    $54.89bn          60
#   TigerGlobal        52         443             54    $22.85bn           0
#   Elliott (current)  25         107             17    $15.90bn           0   CIK 1791786, 2020-03-31 on
#   Elliott (pred.)    30         277              1    $0               0   CIK 1048445, dormant by 2026
#
# Two Sigma Advisers LP (CIK 1478735) is DELIBERATELY EXCLUDED -- measured (phase0b) latest table is
# 1 name at $0 (dormant, same pattern as Elliott's predecessor once superseded) and its full-history
# CUSIP set adds only 7 incremental CUSIPs over what Two Sigma Investments (1179392) already
# contributes. Not a second book, not an oversight -- do not add it back without re-measuring.
MANAGERS = [
    {"book": "Soros",       "cik": 1029160,
     "name": "Soros Fund Management LLC",              "type": "Long/short equity"},
    {"book": "Bridgewater", "cik": 1350694,
     "name": "Bridgewater Associates, LP",              "type": "Global macro"},
    {"book": "Citadel",     "cik": 1423053,
     "name": "Citadel Advisors LLC",                    "type": "Multi-strategy"},
    {"book": "Millennium",  "cik": 1273087,
     "name": "Millennium Management LLC",               "type": "Multi-strategy"},
    {"book": "Renaissance", "cik": 1037389,
     "name": "Renaissance Technologies LLC",            "type": "Systematic quant"},
    {"book": "AQR",         "cik": 1167557,
     "name": "AQR Capital Management LLC",              "type": "Systematic quant / factor"},
    {"book": "TwoSigma",    "cik": 1179392,
     "name": "Two Sigma Investments, LP",                "type": "Systematic quant"},
    {"book": "DEShaw",      "cik": 1009207,
     "name": "D. E. Shaw & Co., Inc.",                   "type": "Multi-strategy quant"},
    {"book": "Point72",     "cik": 1603466,
     "name": "Point72 Asset Management, L.P.",           "type": "Multi-strategy"},
    {"book": "TigerGlobal", "cik": 1167483,
     "name": "Tiger Global Management LLC",              "type": "Long/short equity - growth"},
    {"book": "Elliott",     "cik": (1791786, 1048445),    # current, predecessor -- see stitch_multi_cik
     "name": "Elliott Investment Management L.P. (current) / Elliott Management Corp (predecessor)",
     "type": "Event-driven / activist"},
]
ACTIVE_MANAGERS: list[str] | None = None
# ^ None = run every book in MANAGERS (the eventual default). Set to a list of "book" names (e.g.
# ["Soros", "TigerGlobal"]) to scope build_frames() to a subset -- used for the Phase-1 2-manager
# verification build so a full 11-manager pull isn't required to test the plumbing.

DROP_ETPS = True
# Exclude ETF / index-fund / commodity-crypto-trust rows from every manager's 13F book. Measured
# value share on the latest filing (phase0b recon, word-boundary matched): Bridgewater 24.5%,
# Millennium 11.7%, Citadel 6.7% -- these vehicles carry no XBRL fundamentals and no sector, so they
# would produce meaningless cross-sectional style/industry loadings (an ETF IS a diversified slice of
# the market, not a single-name factor bet, and Barra-style descriptors are not well-defined for it).
# Matched on WORD BOUNDARIES, never substring -- a naive substring match flags "NETFLIX" for "ETF".
# This list deliberately does NOT include bare TRUST, PORTFOLIO or FUND tokens: real operating
# companies carry those words in their legal names (MEDICAL PROPERTIES TRUST, KITE REALTY GROUP
# TRUST, AMERICOLD REALTY TRUST, REDWOOD TRUST, CONSUMER PORTFOLIO SVCS, ALTISOURCE PORTFOLIO
# SOLUTIONS are all real REITs/operating companies seen in the recon sample) -- only brand names /
# phrases that are ALWAYS a pooled vehicle in EDGAR issuer-name conventions are listed. "INVESCO" is
# deliberately scoped to "INVESCO QQQ" (the flagship ETF trust), not bare "INVESCO", because Invesco
# Ltd (ticker IVZ) is itself a real operating company that could legitimately be HELD as a stock.
_ETP_TOKENS = [
    r"\bETF\b", r"\bSPDR\b", r"\bISHARES\b", r"\bVANGUARD\b", r"\bVANECK\b",
    r"\bPROSHARES\b", r"\bKRANESHARES\b", r"\bDIREXION\b", r"\bGRAYSCALE\b", r"\bBITWISE\b",
    r"\bSELECT SECTOR\b", r"\bINVESCO QQQ\b", r"\bINDEX FDS?\b", r"\bTIDAL TRUST\b",
]
_ETP_RE = re.compile("|".join(_ETP_TOKENS))

# Growth dropped 2026-07-04: |t|>2 on only 9% of regression days (the admission bar)
# and a loading on only 21.3% of held weight (asset-growth needs two XBRL vintages).
# RateBeta added 2026-07-04: partial duration beta (stock return on the TLT return residualized
# against the market, 252d window). The audit's Leverage/Liquidity/MegaCap flags shared one
# carrier list (hto/tsm/ida/cms/amzn — utilities, a REIT, rate-sensitive mega-caps): correlated
# residuals with one theme = a missing rates/duration factor.
# NdxBeta added 2026-07-04: partial mega-complex beta (QQQ residualized against the market,
# same construction) — the mega complex co-moves as a GROUP beyond any smooth function of
# log-mcap, and tsm's imputed size can't see its home-market volume. A realized-comovement
# descriptor prices the club directly.
# MegaCap (spline knot in the size curve, added earlier on 2026-07-04) was DROPPED the same
# day: NdxBeta took over the club's pricing (MegaCap admission fell to 13%, weakest in the
# model) and its one remaining hidden-beta flag was carried by imputed-size names whose hinge
# loading the log-ADV fit cannot estimate (tsm's Taipei volume) — a descriptor that only
# misprices the names it was built for earns no seat.
STYLE_FACTORS = ["Beta", "Momentum", "Size", "Value", "RateBeta", "NdxBeta",
                 "Leverage", "Liquidity", "ResidVol", "EarnYield", "NonLinSize"]

CACHE_DIR.mkdir(parents=True, exist_ok=True)
SEC_HEADERS = {"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate"}


# --------------------------------------------------------------------------- http helpers (cached, polite)
# Thread-safe so the per-name pulls can be parallelised (see _pull_map). Everything is disk-cached
# under CACHE_DIR keyed by URL md5, so a rerun re-pulls NOTHING -- a warm cache is pure local reads.
# A single global rate gate spaces request *initiations* >= _MIN_INTERVAL apart across ALL threads
# (SEC asks <=10 req/s); the network round-trips still overlap, which is where the speed-up comes
# from. _HTTP counts cache hits vs network fetches so progress output can prove the cache is working.
_MIN_INTERVAL = 0.12
_RATE_LOCK = threading.Lock()
_NEXT_AT = [0.0]
_HTTP_LOCK = threading.Lock()
_HTTP = {"hit": 0, "miss": 0}

def _cache_path(url: str) -> pathlib.Path:
    return CACHE_DIR / (hashlib.md5(url.encode()).hexdigest() + ".bin")

def _throttle(interval: float) -> None:
    """Block until this thread is cleared to initiate a request, globally rate-limited."""
    with _RATE_LOCK:
        now = time.monotonic()
        wait = _NEXT_AT[0] - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _NEXT_AT[0] = now + max(interval, _MIN_INTERVAL)

def _get(url: str, headers=None, sleep=0.12) -> bytes:
    p = _cache_path(url)
    if p.exists():
        with _HTTP_LOCK:
            _HTTP["hit"] += 1
        return p.read_bytes()
    _throttle(sleep)
    r = requests.get(url, headers=headers or SEC_HEADERS, timeout=30)
    r.raise_for_status()
    p.write_bytes(r.content)
    with _HTTP_LOCK:
        _HTTP["miss"] += 1
    return r.content

def _get_json(url, headers=None):
    return json.loads(_get(url, headers))

def _post_json(url: str, payload, headers=None, sleep=0.0) -> bytes:
    """POST with the same disk cache as _get; keyed on url + request body."""
    body = json.dumps(payload, sort_keys=True)
    p = _cache_path(url + "|" + body)
    if p.exists():
        with _HTTP_LOCK:
            _HTTP["hit"] += 1
        return p.read_bytes()
    _throttle(sleep)
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    p.write_bytes(r.content)
    with _HTTP_LOCK:
        _HTTP["miss"] += 1
    return r.content


# --------------------------------------------------------------------------- parallel pulls + progress
PULL_WORKERS = int(os.environ.get("PULL_WORKERS", "8"))   # threads for the per-name data pulls

def _progress(label: str, done: int, n: int, every: int = 25) -> None:
    """Print a status line every `every` items (and at the end) so a long pull is monitorable.
    Shows cumulative network fetches vs cache hits, proving reruns hit disk, not the network."""
    if done == n or done % every == 0:
        print(f"  [{label:12s}] {done:4d}/{n:<4d}   http: {_HTTP['miss']} fetched / "
              f"{_HTTP['hit']} cached", flush=True)

def _pull_map(fn, items, label, workers: int = PULL_WORKERS) -> dict:
    """Run fn over items on a thread pool, reporting progress as each completes. Returns
    {item: result}; an item whose fetch raises maps to None (same as the old comprehensions)."""
    items = list(items)
    n = len(items)
    out, done = {}, 0
    print(f"  [{label:12s}] pulling {n} (workers={workers}) ...", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fn, it): it for it in items}
        for fut in as_completed(futs):
            it = futs[fut]
            try:
                out[it] = fut.result()
            except Exception:
                out[it] = None
            done += 1
            _progress(label, done, n)
    return out


# --------------------------------------------------------------------------- SIC -> GICS sector
# Free GICS sector proxy: SEC assigns every filer a 4-digit SIC code (in the submissions JSON,
# CIK-keyed); we crosswalk SIC -> one of the 11 GICS sectors. Ranges are ordered SPECIFIC -> BROAD
# and the FIRST containing range wins, so narrow overrides (pharma inside chemicals, REITs inside
# financial offices, software inside business services, autos inside transport equipment) must
# precede the broad bucket they sit in. Approximate but standard and fully reproducible offline.
_SIC_GICS: list[tuple[int, int, str]] = [
    # --- Health Care (override: pharma/devices/services sit inside chemicals/instruments/services)
    (2833, 2836, "Health Care"),            # medicinal/biological/pharmaceutical
    (3840, 3851, "Health Care"),            # surgical/medical/dental instruments
    (8000, 8099, "Health Care"),            # health services
    (5047, 5047, "Health Care"),            # medical equipment wholesale
    (5912, 5912, "Consumer Staples"),       # drug stores (retail staple, not health care)
    # --- Consumer Staples
    (2000, 2099, "Consumer Staples"),       # food & kindred (incl beverages 2080s)
    (2100, 2199, "Consumer Staples"),       # tobacco
    (2840, 2844, "Consumer Staples"),       # soap/detergents/toiletries
    (5400, 5499, "Consumer Staples"),       # food stores
    (5140, 5149, "Consumer Staples"),       # groceries wholesale
    # --- Agriculture services (override before the broad farming bucket below)
    (700, 799, "Industrials"),              # agricultural services (landscaping, etc.)
    # --- Energy
    (1200, 1299, "Energy"),                 # coal
    (1300, 1399, "Energy"),                 # oil & gas extraction
    (2900, 2999, "Energy"),                 # petroleum refining
    (4610, 4619, "Energy"),                 # pipelines
    # --- Materials
    (1000, 1099, "Materials"),              # metal mining (incl gold 1040)
    (1400, 1499, "Materials"),              # nonmetallic mining
    (2400, 2499, "Materials"),              # lumber & wood
    (2600, 2699, "Materials"),              # paper
    (2800, 2829, "Materials"),              # industrial chemicals (pharma carved out above)
    (2837, 2899, "Materials"),              # other chemicals (ex pharma/household)
    (3200, 3299, "Materials"),              # stone, clay, glass
    (3300, 3399, "Materials"),              # primary metal
    # --- Information Technology
    (3570, 3579, "Information Technology"),  # computers & office equipment
    (3670, 3679, "Information Technology"),  # semiconductors & electronic components
    (3661, 3669, "Information Technology"),  # communications equipment
    (7370, 7379, "Information Technology"),  # computer programming, software, data services
    (3812, 3812, "Information Technology"),  # search/detection/navigation systems
    # --- Communication Services
    (4800, 4899, "Communication Services"),  # telecom (telephone, radio, TV, cable)
    (2700, 2799, "Communication Services"),  # publishing & printing
    (7300, 7319, "Communication Services"),  # advertising/PR (7310s) — before business-svcs bucket
    (7800, 7849, "Communication Services"),  # motion pictures
    (3651, 3652, "Communication Services"),  # household audio/video, prerecorded media
    # --- Utilities
    (4900, 4999, "Utilities"),               # electric, gas, water, sanitary
    # --- Real Estate (override: REITs & operators sit inside the 6xxx finance block)
    (6500, 6599, "Real Estate"),             # real estate operators/developers
    (6798, 6798, "Real Estate"),             # REITs
    # --- Financials
    (6000, 6199, "Financials"),              # depository & non-depository credit
    (6200, 6299, "Financials"),              # security & commodity brokers
    (6300, 6499, "Financials"),              # insurance
    (6700, 6799, "Financials"),              # holding & investment offices (ex REITs above)
    # --- Consumer Discretionary
    (2300, 2399, "Consumer Discretionary"),  # apparel
    (2500, 2599, "Consumer Discretionary"),  # furniture & fixtures
    (3000, 3199, "Consumer Discretionary"),  # rubber, plastics, leather, footwear
    (3700, 3716, "Consumer Discretionary"),  # motor vehicles & parts
    (3900, 3999, "Consumer Discretionary"),  # toys, sporting goods, jewelry, misc
    (5200, 5999, "Consumer Discretionary"),  # retail (food/drug carved out above)
    (5500, 5599, "Consumer Discretionary"),  # auto dealers
    (7000, 7299, "Consumer Discretionary"),  # hotels & personal services
    (7500, 7599, "Consumer Discretionary"),  # auto rental/repair & misc repair services
    (7900, 7999, "Consumer Discretionary"),  # amusement & recreation
    (8200, 8299, "Consumer Discretionary"),  # educational services
    (8300, 8399, "Consumer Discretionary"),  # social services (incl child day care)
    (100, 699, "Consumer Staples"),          # farming & livestock production
    # --- Industrials (broad buckets last)
    (1500, 1799, "Industrials"),             # construction
    (3400, 3569, "Industrials"),             # fabricated metal, industrial machinery
    (3580, 3659, "Industrials"),             # general/electrical industrial equipment
    (3680, 3699, "Industrials"),             # other electrical equipment
    (3717, 3799, "Industrials"),             # aerospace, ships, rail, other transport equip
    (3800, 3829, "Industrials"),             # instruments (ex 3812 IT above)
    (4000, 4799, "Industrials"),             # transportation services (rail, air, trucking)
    (5000, 5199, "Industrials"),             # durable-goods wholesale
    (7320, 7399, "Industrials"),             # business services (ex software/advertising above)
    (8700, 8799, "Industrials"),             # engineering, accounting, management services
]


def sic_to_gics(sic) -> str:
    """Map a 4-digit SIC code to a GICS sector (first containing range wins); else 'Unknown'."""
    try:
        code = int(sic)
    except (TypeError, ValueError):
        return "Unknown"
    for lo, hi, sector in _SIC_GICS:
        if lo <= code <= hi:
            return sector
    return "Unknown"


def sectors_for_ciks(ciks) -> pd.DataFrame:
    """For each CIK, fetch its SEC SIC (submissions JSON, disk-cached) and crosswalk to GICS.

    Returns (CIK, SIC, SICDesc, Sector). Network failures degrade to 'Unknown', never raise."""
    def _one(cik):
        cik = int(cik)
        try:
            j = _get_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
            sic, desc = j.get("sic"), j.get("sicDescription")
        except Exception:
            sic, desc = None, None
        return {"CIK": cik, "SIC": sic, "SICDesc": desc, "Sector": sic_to_gics(sic)}
    uniq = [int(c) for c in pd.unique(pd.Series(list(ciks)).dropna())]
    res = _pull_map(_one, uniq, "sectors")
    return pd.DataFrame([res[c] for c in uniq])


# SEC 2-letter codes that denote a US state / territory; anything else in stateOfIncorporation is a
# foreign domicile (e.g. K3 Hong Kong, E9 Cayman, A6 Ontario, I0 France).
US_STATE_CODES = frozenset(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ "
    "NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR GU VI AS MP".split())


def _norm_country(desc) -> str:
    """Collapse an SEC domicile description to a country: Canadian provinces / federal -> Canada;
    everything else kept as the SEC description (e.g. 'France', 'Hong Kong', 'Cayman Islands')."""
    d = (desc or "").strip()
    if not d:
        return ""
    return "Canada" if "Canada" in d else d


def countries_for_ciks(ciks) -> pd.DataFrame:
    """Country of incorporation per CIK, from the SAME SEC submissions JSON sectors_for_ciks reads
    (disk-cached, no extra network on a warm cache). A US state code -> 'US'; a foreign
    stateOfIncorporation -> that country; a blank field falls back to the business-address country.
    Network failure / no domicile -> 'US' (the safe default for a US-listed 13F). Residual mislabels
    are ADRs that file a US business address with a blank domicile (e.g. Grifols) — all tiny weights."""
    def _one(cik):
        cik = int(cik)
        try:
            j = _get_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
            soi = (j.get("stateOfIncorporation") or "").strip()
            if soi in US_STATE_CODES:
                country = "US"
            elif soi:                                   # non-blank, non-US state -> foreign
                country = _norm_country(j.get("stateOfIncorporationDescription")) or "US"
            else:                                       # blank domicile -> business address
                biz = (j.get("addresses") or {}).get("business") or {}
                country = ("US" if (biz.get("stateOrCountry") or "").strip() in US_STATE_CODES
                           else _norm_country(biz.get("stateOrCountryDescription")) or "US")
        except Exception:
            country = "US"
        return {"CIK": cik, "Country": country}
    uniq = [int(c) for c in pd.unique(pd.Series(list(ciks)).dropna())]
    res = _pull_map(_one, uniq, "countries")
    return pd.DataFrame([res[c] for c in uniq])


# --------------------------------------------------------------------------- 1. 13F positions (multi-manager)
_FILING_COLS = ("form", "accessionNumber", "filingDate", "reportDate", "primaryDocument")


def _13FHR_filings_all(cik: int) -> pd.DataFrame:
    """All 13F-HR filing metadata for `cik`: `filings.recent` PLUS every `filings.files` pagination
    block, de-duplicated by accessionNumber. SEC caps `recent` at ~1000 entries of ANY form type, so
    a CIK with heavy non-13F filing volume can have its older 13F-HRs pushed out of `recent` into the
    paginated blocks even though it filed 13F-HR continuously (measured: Renaissance's `recent` alone
    starts 2019-12-31, missing 2016-2019 entirely; Citadel's misses the first ~6 weeks of 2016 -- see
    scratchpad/phase0-recon.md Task B). Uses the same cached `_get_json` as everything else, so a
    rerun costs nothing extra."""
    sub = _get_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    recent = sub["filings"]["recent"]
    frames = [pd.DataFrame({k: recent[k] for k in _FILING_COLS})]
    for blk in sub["filings"].get("files", []):
        url = f"https://data.sec.gov/submissions/{blk['name']}"
        try:
            data = _get_json(url)
        except Exception:
            continue
        if not isinstance(data, dict) or "form" not in data:
            continue
        fb = pd.DataFrame({k: data.get(k, [None] * len(data["form"])) for k in _FILING_COLS})
        frames.append(fb)
    allf = pd.concat(frames, ignore_index=True)
    allf = allf[allf["form"] == "13F-HR"].drop_duplicates(subset="accessionNumber")
    return allf


def positions_from_13f(cik: int) -> pd.DataFrame:
    """Parse every 13F-HR information table for `cik` (recent + paginated filings.files blocks)
    into (report_date, cusip, issuer, shares, value). Return contract is unchanged from the
    single-CIK, recent-only version -- only the ROW set grows (older filings recovered via
    pagination); no new columns."""
    f = _13FHR_filings_all(cik)
    rows = []
    for _, r in f.iterrows():
        acc_nodash = r["accessionNumber"].replace("-", "")
        idx = _get_json(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/index.json")
        # the information table is the .xml document that is NOT the primary (cover) doc
        xmls = [it["name"] for it in idx["directory"]["item"]
                if it["name"].lower().endswith(".xml") and "primary_doc" not in it["name"].lower()]
        if not xmls:
            continue
        xml = _get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{xmls[-1]}")
        rows += _parse_infotable(xml, r["reportDate"], r["filingDate"])
    df = pd.DataFrame(rows)
    # cash equities only for the 2-block model: drop options (putCall set) and bonds (PRN)
    df = df[(df["putCall"].fillna("") == "") & (df["sshType"].fillna("SH") == "SH")]
    df["cusip"] = df["cusip"].str.upper().str.strip()
    return df

def _parse_infotable(xml_bytes: bytes, report_date: str, filing_date: str) -> list[dict]:
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml_bytes)
    out = []
    def lt(e, tag):                                     # namespace-agnostic local-tag lookup
        for c in e.iter():
            if c.tag.rsplit("}", 1)[-1] == tag and c.text:
                return c.text.strip()
        return None
    for it in [e for e in root.iter() if e.tag.rsplit("}", 1)[-1] == "infoTable"]:
        out.append({
            "report_date": pd.Timestamp(report_date),
            "filing_date": pd.Timestamp(filing_date),
            "issuer":  lt(it, "nameOfIssuer"),
            "cusip":   lt(it, "cusip"),
            "value":   float(lt(it, "value") or 0),     # weights are scale-free, so $ vs $000 is moot
            "shares":  float(lt(it, "sshPrnamt") or 0),
            "sshType": lt(it, "sshPrnamtType"),
            "putCall": lt(it, "putCall"),
        })
    return out


def stitch_multi_cik(dfs: list[pd.DataFrame]) -> pd.DataFrame:
    """Concatenate positions_from_13f frames for CIKs that are really ONE manager (an entity
    rename/re-filing, e.g. Elliott's 2020 transition from CIK 1048445 to 1791786). `dfs` are given
    in PREFERENCE order -- the first entity wins on any report_date the others also cover. Pure /
    no network, so it's independently unit-testable against the documented Elliott overlap (both
    CIKs filed for 2020-03-31, 2020-06-30, 2020-09-30)."""
    out, covered = [], set()
    for df in dfs:
        if df is None or df.empty:
            continue
        keep = df[~df["report_date"].isin(covered)]
        if not keep.empty:
            out.append(keep)
        covered |= set(df["report_date"].unique())
    if not out:
        return pd.DataFrame(columns=["report_date", "filing_date", "issuer", "cusip",
                                     "value", "shares", "sshType", "putCall"])
    return pd.concat(out, ignore_index=True)


def _is_etp_issuer(issuer) -> bool:
    """Word-boundary ETP/fund-vehicle match on a 13F issuer name. See DROP_ETPS above for the
    token-selection rationale and the false-positive cases (REITs/operating cos named *TRUST /
    *PORTFOLIO) it's deliberately tuned to avoid."""
    return bool(_ETP_RE.search((issuer or "").upper()))


def filter_etps(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Drop ETP/fund-like rows (see DROP_ETPS) from a positions_from_13f-shaped frame. Returns
    (kept_df, disclosure) -- the drop is NEVER silent; disclosure carries both the full-history drop
    counts and a latest-filing value share (a stable "how much of TODAY's book is ETPs" snapshot,
    matching how phase0b recon measured it -- an all-history value share would be diluted by a
    decade of size changes and mean less). disclosure is safe to persist even when df is empty."""
    cols = ["report_date", "filing_date", "issuer", "cusip", "value", "shares", "sshType", "putCall"]
    if df.empty:
        return df, {"latest_report_date": None, "n_dropped_rows_all_history": 0,
                    "n_dropped_cusips_all_history": 0, "n_dropped_latest": 0,
                    "dropped_value_latest": 0.0, "total_value_latest": 0.0,
                    "dropped_value_share_latest": 0.0}
    if not DROP_ETPS:
        latest = df["report_date"].max()
        return df, {"latest_report_date": str(latest.date()), "n_dropped_rows_all_history": 0,
                    "n_dropped_cusips_all_history": 0, "n_dropped_latest": 0,
                    "dropped_value_latest": 0.0,
                    "total_value_latest": float(df.loc[df["report_date"] == latest, "value"].sum()),
                    "dropped_value_share_latest": 0.0}
    is_etp = df["issuer"].apply(_is_etp_issuer)
    latest = df["report_date"].max()
    ldf = df[df["report_date"] == latest]
    l_is_etp = ldf["issuer"].apply(_is_etp_issuer)
    total_value_latest = float(ldf["value"].sum())
    dropped_value_latest = float(ldf.loc[l_is_etp, "value"].sum())
    disclosure = {
        "latest_report_date": str(latest.date()),
        "n_dropped_rows_all_history": int(is_etp.sum()),
        "n_dropped_cusips_all_history": int(df.loc[is_etp, "cusip"].nunique()),
        "n_dropped_latest": int(l_is_etp.sum()),
        "dropped_value_latest": dropped_value_latest,
        "total_value_latest": total_value_latest,
        "dropped_value_share_latest": (dropped_value_latest / total_value_latest
                                       if total_value_latest else 0.0),
    }
    return df.loc[~is_etp, cols].copy(), disclosure


# --------------------------------------------------------------------------- 2. OpenFIGI crosswalk
# --- persistent per-CUSIP crosswalk ----------------------------------------------------
# The HTTP cache cannot protect the crosswalk. `_post_json` keys on url+BODY, and the bodies here
# are batches cut from `sorted(set(cusips))` -- so changing the universe (adding a manager, say)
# re-cuts every batch, misses every key, and re-asks OpenFIGI live. That matters because OpenFIGI
# ANSWERS DRIFT: measured 2026-07-30, the 11-manager build lost 6 real Soros holdings worth 7.1%
# of the book (Honeywell, Chart Industries, Clearwater, Select Medical, DuPont, National Storage)
# purely because CUSIPs that resolved for the single-book build now return "No identifier found".
# So resolutions are cached PER CUSIP and a positive answer is never overwritten by a later
# negative -- a name that resolved once stays resolved, and rebuilds stop silently deleting
# holdings. Negatives are stored too but always re-queried (cheap: ~180 batches, under a minute
# with a key), so a genuinely new listing can still resolve on a later run.
_XWALK_CACHE = CACHE_DIR / "cusip_crosswalk.json"

def _load_xwalk() -> dict:
    """Persistent cusip -> {figi, ticker, name}. Seeded from the last built securities frame so
    the mapping survives even a cold `tmp/`."""
    m: dict = {}
    if _XWALK_CACHE.exists():
        try:
            m = json.loads(_XWALK_CACHE.read_text())
        except Exception:
            m = {}
    sec_p = DATA_DIR / "securities.parquet"
    if sec_p.exists():
        try:
            s = pd.read_parquet(sec_p)
            for cu, fg, tk in zip(s["CUSIP"], s["Position"], s["Ticker"]):
                cu = str(cu).upper().strip()
                if cu and pd.notna(fg) and not (m.get(cu) or {}).get("figi"):
                    m[cu] = {"figi": str(fg), "ticker": str(tk or "").lower(), "name": None}
        except Exception:
            pass
    return m

def _save_xwalk(m: dict) -> None:
    _XWALK_CACHE.parent.mkdir(parents=True, exist_ok=True)
    _XWALK_CACHE.write_text(json.dumps(m, sort_keys=True))

def crosswalk_cusips(cusips: list[str]) -> pd.DataFrame:
    """CUSIP -> FIGI/ticker via OpenFIGI v3. Batches of 10 (no key) / 100 (with key).

    Only CUSIPs with no POSITIVE cached resolution are sent; see the _XWALK_CACHE note above."""
    url = "https://api.openfigi.com/v3/mapping"
    hdr = {"Content-Type": "application/json"}
    if OPENFIGI_KEY:
        hdr["X-OPENFIGI-APIKEY"] = OPENFIGI_KEY
    batch = 100 if OPENFIGI_KEY else 10
    uniq = sorted(set(c for c in cusips if c))
    cache = _load_xwalk()
    todo = [c for c in uniq if not (cache.get(c) or {}).get("figi")]
    print(f"  crosswalk: {len(uniq)} cusips, {len(uniq) - len(todo)} already resolved, "
          f"{len(todo)} to query", flush=True)
    kept = 0
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        jobs = [{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"} for c in chunk]
        resp = json.loads(_post_json(url, jobs, headers=hdr,
                                     sleep=0.3 if OPENFIGI_KEY else 2.6))  # respect 25/min unauth limit
        for c, res in zip(chunk, resp):
            d = (res.get("data") or [{}])[0]
            figi = d.get("compositeFIGI") or d.get("figi")
            if figi:
                cache[c] = {"figi": figi, "ticker": (d.get("ticker") or "").lower(),
                            "name": d.get("name")}
            elif (cache.get(c) or {}).get("figi"):
                kept += 1                      # live says "not found", cache says otherwise: keep
            else:
                cache.setdefault(c, {"figi": None, "ticker": "", "name": None})
    _save_xwalk(cache)
    if kept:
        print(f"  crosswalk: kept {kept} cached resolutions OpenFIGI no longer returns", flush=True)
    empty = {"figi": None, "ticker": "", "name": None}
    return pd.DataFrame([{"cusip": c, **(cache.get(c) or empty)} for c in uniq])

def ticker_to_cik() -> pd.DataFrame:
    j = _get_json("https://www.sec.gov/files/company_tickers.json")
    return pd.DataFrame([{"ticker": v["ticker"].lower(), "cik": int(v["cik_str"]),
                          "title": v["title"]} for v in j.values()])

def index_constituents() -> list[str]:
    """Market-index seed for the estimation universe (lowercased Stooq-style tickers).

    A real Barra cross-section is a broad market, not one manager's 13F book; SEED_INDEX picks
    which index. Fetched through _get so the constituent file is disk-cached like every other
    pull -- no re-pull on rerun. Class tickers (BRK.B) use '-' on Stooq/Yahoo, so '.'->'-'."""
    if not SEED_INDEX:
        return []
    raw = _get(SP500_URL, headers={"User-Agent": "Mozilla/5.0"})
    df = pd.read_csv(io.BytesIO(raw))
    col = "Symbol" if "Symbol" in df.columns else df.columns[0]
    return [str(t).strip().lower().replace(".", "-") for t in df[col].dropna() if str(t).strip()]


# --------------------------------------------------------------------------- 3. Stooq prices
def _yahoo_daily(symbol: str) -> pd.DataFrame | None:
    """Fallback when Stooq is unreachable: full daily history from the Yahoo chart API,
    shaped to the same contract as stooq_daily (Date index, Close, Volume)."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol.upper()}"
           "?period1=0&period2=9999999999&interval=1d")
    try:
        res = json.loads(_get(url, headers={"User-Agent": "Mozilla/5.0"}, sleep=0.5))["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        adj = res["indicators"].get("adjclose", [{}])[0].get("adjclose")
        df = pd.DataFrame({"Date": pd.to_datetime(res["timestamp"], unit="s").normalize(),
                           "Close": adj if adj is not None else q["close"],
                           "Volume": q["volume"]}).dropna(subset=["Close"])
    except Exception:
        return None
    if df.empty:
        return None
    return df.set_index("Date").sort_index()

def stooq_daily(symbol: str) -> pd.DataFrame | None:
    url = f"https://stooq.com/q/d/l/?s={symbol}.us&i=d"
    try:
        raw = _get(url, headers={"User-Agent": SEC_UA})
        if raw.lstrip()[:1] == b"<":            # JS-challenge / error page, not CSV
            return _yahoo_daily(symbol)
        df = pd.read_csv(io.BytesIO(raw))
    except Exception:
        return _yahoo_daily(symbol)
    if df.empty or "Close" not in df:
        return _yahoo_daily(symbol)
    df["Date"] = pd.to_datetime(df["Date"])
    return df.set_index("Date").sort_index()

def price_descriptors(prices: dict[str, pd.DataFrame], cal: pd.DatetimeIndex,
                      mkt: pd.DataFrame, rates: pd.DataFrame | None = None,
                      ndx: pd.DataFrame | None = None) -> pd.DataFrame:
    """Beta, ResidVol, Momentum, RateBeta, NdxBeta, raw log-ADV (the Liquidity input) from
    daily prices.

    Each descriptor carries its OWN history gate — the beta family needs 120 return days,
    Momentum 252, the ADV mean only 21 volume days — so a young listing gets the loadings
    its history supports instead of none at all (the old single 120-day gate silently
    dropped every price descriptor for post-IPO entrants the book actually held).

    RateBeta / NdxBeta are PARTIAL betas: the proxy's (TLT / QQQ) daily return is residualized
    against the market over the same window, then the stock's beta to that residual — so each
    measures sensitivity BEYOND what equity beta already carries, not a second Beta.
    `rates=None` / `ndx=None` (older call sites) emit the column as NaN."""
    mret = mkt["Close"].pct_change()
    rret = rates["Close"].pct_change() if rates is not None else None
    nret = ndx["Close"].pct_change() if ndx is not None else None
    recs = []
    for tkr, px in prices.items():
        ret = px["Close"].pct_change()
        dvol = (px["Close"] * px.get("Volume", np.nan))
        for d in cal:
            win = ret.loc[:d].tail(252)
            dv = dvol.loc[:d].dropna().tail(63)
            have_ret, have_dv = len(win) >= 120, len(dv) >= 21
            if not (have_ret or have_dv):
                continue
            rateb = ndxb = np.nan
            if have_ret:
                m = mret.reindex(win.index).fillna(0.0)
                if m.std() > 0:
                    beta = np.cov(win.fillna(0), m)[0, 1] / m.var()
                    rvol = (win.fillna(0) - beta * m).std() * np.sqrt(252)
                else:
                    beta, rvol = np.nan, win.std() * np.sqrt(252)
                for src, out_name in ((rret, "rateb"), (nret, "ndxb")):
                    if src is not None and m.std() > 0:
                        r = src.reindex(win.index).fillna(0.0)
                        if r.std() > 0:
                            rr = r - (np.cov(r, m)[0, 1] / m.var()) * m   # proxy ⊥ market
                            if rr.var() > 0:
                                b_ = np.cov(win.fillna(0), rr)[0, 1] / rr.var()
                                if out_name == "rateb":
                                    rateb = b_
                                else:
                                    ndxb = b_
            else:
                beta = rvol = np.nan
            # LOG relative strength (USE4 RSTR), not arithmetic return: the arithmetic ratio is
            # bounded at -1 and unbounded above, so its z-scored winner tail runs to +8/+10 while
            # realized momentum sensitivity saturates near +1 (the 2026-07-04 audit's Momentum
            # hidden beta, gamma ~ -0.4 on the winner side only). log symmetrizes the tail.
            mom = (float(np.log(px["Close"].loc[:d].iloc[-21] / px["Close"].loc[:d].iloc[-252]))
                   if len(px.loc[:d]) >= 252 else np.nan)
            recs.append({"ticker": tkr, "Date": d, "Beta": beta,
                         "ResidVol": rvol,
                         "Momentum": mom,
                         "RateBeta": rateb,
                         "NdxBeta": ndxb,
                         "Liquidity": np.log(dv.mean() + 1) if have_dv else np.nan})
    return pd.DataFrame(recs)


# --------------------------------------------------------------------------- 4. EDGAR XBRL fundamentals (PIT)
_TAGS = {"Assets": "Assets", "Liabilities": "Liabilities",
         "Equity": "StockholdersEquity", "NetIncome": "NetIncomeLoss",
         "Shares": "CommonStockSharesOutstanding"}

def fundamentals(cik: int) -> pd.DataFrame | None:
    """Point-in-time fundamentals: one row per (filed-date) carrying the latest reported values."""
    try:
        facts = _get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json")
    except Exception:
        return None
    gaap = facts.get("facts", {}).get("us-gaap", {})
    dei  = facts.get("facts", {}).get("dei", {})
    series = {}
    for name, tag in _TAGS.items():
        src = gaap.get(tag, {})
        if name == "Shares":
            # us-gaap:CommonStockSharesOutstanding is unreliable: filings carry it in
            # par-value / per-class contexts, so the "latest" fact can be 0 or 100 shares
            # (observed: CVNA=0, CHWY=100), which collapses mcap and turns the Value /
            # EarnYield *ratios* into raw dollars. Prefer the cover-page DEI tag, which
            # is the actual entity-level share count.
            src = dei.get("EntityCommonStockSharesOutstanding") or src
        units = src.get("units", {})
        vals = next(iter(units.values()), [])           # USD or shares unit
        s = pd.DataFrame(vals)
        if s.empty or "filed" not in s:
            continue
        s = s.dropna(subset=["val"]).assign(filed=lambda x: pd.to_datetime(x["filed"]))
        series[name] = s[["filed", "val"]].rename(columns={"val": name})
    if "Equity" not in series:
        return None
    out = series["Equity"]
    for name, s in series.items():
        if name != "Equity":
            out = pd.merge_asof(out.sort_values("filed"), s.sort_values("filed"),
                                on="filed", direction="backward")
    for name in _TAGS:                                  # companies may lack a tag entirely;
        if name not in out:                             # guarantee the schema so callers can
            out[name] = np.nan                          # index columns unconditionally
    return out.sort_values("filed").drop_duplicates("filed", keep="last")


# --------------------------------------------------------------------------- 5. assemble exposures (the leaf)
def _winsor_z(s: pd.Series) -> pd.Series:
    """Robust cross-sectional z-score: centre/scale by median/MAD, winsorise at ±3σ,
    then re-standardise to mean 0 / std 1.

    Why not quantile clipping (the old `clip(q01, q99)` + mean/std): with n≈164 the 1%
    quantile interpolates *between the two most extreme order statistics*, so a
    wrong-units outlier (e.g. a 1e9 "ratio" from a corrupt mcap) survives the clip,
    blows up the std, and squashes all clean names into a near-constant column —
    which is then collinear with the regression intercept (condition number ~1e8 and
    factor returns in the millions). Median/MAD ignore the outlier entirely; the ±3σ
    clip caps it at a finite, harmless loading.
    """
    med = s.median()
    scale = 1.4826 * (s - med).abs().median()           # MAD -> sigma (normal consistency)
    if not scale > 1e-12:                               # degenerate cross-section: no information
        return pd.Series(np.nan, index=s.index)
    z = ((s - med) / scale).clip(-3, 3)
    return (z - z.mean()) / (z.std() + 1e-12)


def _split_z(s: pd.Series, is_est: pd.Series) -> pd.Series:
    """Estimation/coverage standardization (Chris's data-quality point). Centre/scale by the
    ESTIMATION sub-cross-section's median/MAD, winsorise the estimation rows at ±3 (so a corrupt or
    extreme descriptor can't blow up the factor-return regression) — but leave the COVERAGE rows
    UNCAPPED. A held name far below anything in the estimation universe then reads its true large
    loading (e.g. a tiny name's Size at −6), which is the correct statement, not a clip to −3. Both
    sets are re-centred on the estimation post-clip mean/std so they share one scale. Falls back to the
    full cross-section when the estimation subset is too thin to standardise against (or when
    UNCAP_COVERAGE is off, since then every row is flagged estimation)."""
    s = s.astype(float).replace([np.inf, -np.inf], np.nan)
    if is_est.sum() < 10:                               # too few estimation names: use all as reference
        is_est = pd.Series(True, index=s.index)
    est = s[is_est]
    med = est.median()
    scale = 1.4826 * (est - med).abs().median()          # MAD -> sigma, estimation-only
    if not scale > 1e-12:
        return pd.Series(np.nan, index=s.index)
    z = (s - med) / scale
    # estimation rows winsorized at ±3; coverage rows clipped only at the loose COVERAGE_CAP backstop
    # (genuine large tilts survive, corrupt-data blowups don't).
    z_out = z.clip(-3, 3).where(is_est, z.clip(-COVERAGE_CAP, COVERAGE_CAP))
    ref = z.clip(-3, 3)[is_est]                          # estimation post-clip -> the common scale
    mu, sd = ref.mean(), ref.std()
    if not sd > 1e-12:
        return pd.Series(np.nan, index=s.index)
    return (z_out - mu) / sd


def build_exposures(sec: pd.DataFrame, prices: dict, funda: dict,
                    cal: pd.DatetimeIndex, mkt: pd.DataFrame,
                    rates: pd.DataFrame | None = None,
                    ndx: pd.DataFrame | None = None) -> pd.DataFrame:
    pdsc = price_descriptors(prices, cal, mkt, rates, ndx)
    # fundamental descriptors, point-in-time aligned to each calendar date per name
    frecs = []
    for _, row in sec.iterrows():
        f = funda.get(row["cik"])
        px = prices.get(row["ticker"])
        if f is None or px is None:
            continue
        cl = px["Close"].reindex(cal, method="ffill")
        fa = pd.merge_asof(pd.DataFrame({"filed": cal}), f, on="filed", direction="backward")
        fa.index = cal
        mcap = cl * fa["Shares"]
        # Sanity floor: a corrupt XBRL share count (0 / 100 shares) makes mcap ~ 0 and the
        # "+1" in the ratio denominators below would silently report Equity/NetIncome in
        # *raw dollars* (1e9 vs a normal ratio of ~0.05) — one such name dominates the
        # cross-sectional std and flattens every other name's z-score to a constant.
        # Better no descriptor than a wrong-units one: NaN drops the name for that date.
        mcap = mcap.where(mcap > MCAP_FLOOR)
        frecs.append(pd.DataFrame({
            "ticker": row["ticker"], "Date": cal,
            "Size": np.log(mcap + 1),
            "NonLinSize": np.log(mcap + 1),              # cubed AFTER standardization (USE4)
            "Value": fa["Equity"].values / (mcap.values + 1),
            "EarnYield": fa["NetIncome"].values / (mcap.values + 1),
            # Leverage = Liabilities/Assets (2026-07-04 respec). Assets/(Equity+1) is unbounded
            # as equity→0 (insurers 40×, buyback/distressed names 100×+), so the winsor pinned
            # 28 coverage names at one loading and turned the factor into a financials-sector
            # dummy. Liab/A = 1 − 1/(A/E): same information, bounded ~[0,1], differentiates
            # within the block A/E clipped flat. Liabilities falls back to Assets − Equity.
            "Leverage": (fa["Liabilities"].where(fa["Liabilities"].notna(),
                                                 fa["Assets"] - fa["Equity"]).values
                         / (fa["Assets"].values + 1)),
        }))
    fund = pd.concat(frecs, ignore_index=True) if frecs else pd.DataFrame()
    raw = pdsc.merge(fund, on=["ticker", "Date"], how="outer")
    # Liquidity = TURNOVER, not raw dollar volume. log-ADV is essentially log-mcap
    # cross-sectionally (the two factor returns ran ρ ≈ −0.8 vs Size), so the old descriptor
    # was a second Size and the regression split one small/illiquid effect across two unstable
    # coefficients — the book's residual then loaded on "Liquidity" (hidden beta at R² ~0.5).
    # log(ADV+1) − log(mcap+1) ≈ log turnover decorrelates from Size by construction. A name
    # with ADV but no mcap gets NaN — better no descriptor than one in different units.
    if "Liquidity" in raw and "Size" in raw:
        raw["_logadv"] = raw["Liquidity"]                # stash raw log-ADV for size imputation
        both = raw["Liquidity"].notna() & raw["Size"].notna()
        raw["Liquidity"] = (raw["Liquidity"] - raw["Size"]).where(both)
    est_col = sec[["ticker", "figi"]].copy()
    est_col["is_estimation"] = sec["is_estimation"] if "is_estimation" in sec else True
    raw = raw.merge(est_col, on="ticker", how="left").dropna(subset=["figi"])
    raw["is_estimation"] = raw["is_estimation"].fillna(False).astype(bool)

    # cross-sectional standardize each date AGAINST THE ESTIMATION UNIVERSE (estimation rows capped,
    # coverage rows uncapped); orthogonalize NonLinSize to Size on the estimation fit.
    out = []
    n_imputed = 0
    for d, g in raw.groupby("Date"):
        g = g.copy()
        em = g["is_estimation"]
        if {"Size", "_logadv"}.issubset(g):
            # Size-curve imputation for COVERAGE names with prices but no share count (foreign
            # filers report IFRS in native currency and 20-F cover pages count ORDINARY shares
            # against an ADR price — a 5:1 ratio would fake log-mcap by +1.6 — so tag-widening
            # is wrong-units; better a disclosed proxy than either). Impute raw log-mcap from
            # the per-date ESTIMATION regression of log-mcap on log-ADV (ρ ≈ 0.9). Estimation
            # rows are never imputed, so factor-return estimation is untouched — the proxy only
            # lets the held book be priced. Liquidity stays NaN (turnover against its own
            # imputation basis is circular); Value/EarnYield stay NaN (no fundamentals).
            need = (~em) & g["Size"].isna() & g["_logadv"].notna()
            fit = em & g["Size"].notna() & g["_logadv"].notna()
            if need.any() and fit.sum() >= 30:
                b = np.polyfit(g.loc[fit, "_logadv"], g.loc[fit, "Size"], 1)
                imput = b[0] * g.loc[need, "_logadv"] + b[1]
                g.loc[need, "Size"] = imput
                n_imputed += int(need.sum())
        for f in [c for c in STYLE_FACTORS if c in g and c != "NonLinSize"]:
            g[f] = _split_z(g[f].astype(float), em)
        if "Size" in g:
            # NonLinSize = cube of the STANDARDIZED Size (Barra USE4), then the Size-residual.
            # Cubing raw log-mcap (all positive, mean ~24) left a residual dominated by the
            # QUADRATIC term — a U-shape (corr +0.74 with Size²) that pinned small/mid-caps
            # together on its left arm at +6/+7, so their regime landed in residuals (the
            # audit's mid-cap NonLinSize hidden-beta carriers: hto/itri/ida/gtls). Centering
            # first makes the cube genuinely odd: small −, mid ~0, large + — real curvature.
            ref = em if em.sum() >= 10 else pd.Series(True, index=g.index)
            # Cube the WINSORIZED Size (±3, the estimation range), not the uncapped coverage
            # tail (2026-07-04 audit): cubing amplifies a Size error by 3z², so an uncapped
            # coverage Size of −3.6 became a −47 raw cube → a −10 NonLinSize loading with NO
            # realized counterpart (hidden-beta gamma ≈ −1 every era: the loading's predicted
            # comovement fully cancelled in residuals; 35% of held weight sat beyond |3|).
            # The cubic is only meaningful inside the range the estimation fit saw — a deep
            # small-cap reads like the estimation edge, not 3× beyond it. Size itself stays
            # uncapped (linear extrapolation is defensible; cubic extrapolation is not).
            s3 = g["Size"].clip(-3, 3)                    # NaN (no-mcap) stays NaN
            nls = s3 ** 3
            sz = s3.fillna(0.0)
            b = np.polyfit(sz[ref], nls.fillna(0.0)[ref], 1)
            # ... and winsorize the OUTPUT at ±3 for coverage rows too (the cube-residual's
            # MAD-z puts the ±3 Size edge at z ≈ ±9, so the uncapped-coverage rule alone
            # still emitted −9 loadings). Unlike Size, the uncapped tail is not truth here.
            g["NonLinSize"] = _split_z(nls - (b[0] * s3 + b[1]), em).clip(-3, 3)
        if {"Liquidity", "Size"}.issubset(g):
            # Barra-style: orthogonalize Liquidity (turnover) to Size on the estimation fit —
            # raw turnover still shares Size's driver (the turnover respec alone only flipped
            # the factor-return correlation −0.81 → +0.71), so take the Size-residual, the
            # same treatment NonLinSize gets. NaN loadings stay NaN (no-mcap names).
            ref = em if em.sum() >= 10 else pd.Series(True, index=g.index)
            sz, lq = g["Size"].fillna(0.0), g["Liquidity"].fillna(0.0)
            b = np.polyfit(sz[ref], lq[ref], 1)
            g["Liquidity"] = _split_z(g["Liquidity"] - (b[0] * g["Size"] + b[1]), em)
        out.append(g)
    panel = pd.concat(out, ignore_index=True)
    if n_imputed:
        print(f"[exposures] size-curve imputation: {n_imputed} coverage name-dates priced via "
              f"the estimation log-ADV fit (no share count; Liquidity/Value/EarnYield left NaN)")
    long = panel.melt(id_vars=["figi", "Date"], value_vars=STYLE_FACTORS,
                      var_name="Factor", value_name="Loading").dropna(subset=["Loading"])
    return long.rename(columns={"figi": "Position"})


# --------------------------------------------------------------------------- 6. cross-sectional regression
def regress_factors(exp_long: pd.DataFrame, prices: dict,
                    sec: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame,
                                                pd.DataFrame]:
    """Daily WLS of stock returns on the latest prior month-end exposures.

    -> DAILY factor returns (the scenario cache: real 99% tails need ~2,200 obs, not 89)
    -> daily specific variance (EWMA), snapshotted at month-ends for the cube join.
    -> daily specific RETURNS (the un-squared residual u), the 7th frame: the PnL-attribution
       leaf. R_i(t) = L_i·[1;f(t)] + u_i(t) reconstructs the exact return the regression saw,
       so realized = factor + specific ties out to machine precision downstream.
    -> regression stats (Date, Factor, TStat, R2, N): the per-day weighted cross-sectional R²
       and per-factor t-stats — the fit-health diagnostics. A side artifact for /regression,
       NOT one of the seven cube frames.
    All risk numbers downstream are therefore 1-DAY horizon.
    """
    fig2tkr = sec.set_index("figi")["ticker"].to_dict()
    # ESTIMATION/COVERAGE split: factor returns are fit on the ESTIMATION universe only (clean,
    # capped names), but specific residuals are formed for EVERY coverage name against those fitted
    # returns (a held-but-not-estimation name still needs a specific-risk forecast). Falls back to
    # the full cross-section when no estimation flag is present (legacy / UNCAP_COVERAGE off).
    est_pos = set(sec.loc[sec["is_estimation"], "figi"]) if "is_estimation" in sec else set(sec["figi"])
    # GICS industry block (2026-07-04): sector membership drives 0/1 dummies in the daily
    # cross-section — see the constrained-design comment in the monthly loop below.
    fig2sect = sec.set_index("figi")["Sector"].to_dict() if "Sector" in sec else {}
    # Daily return matrix (days x tickers). One-day moves beyond ±50% are treated as data
    # errors (Yahoo adjclose split/glitch artifacts) and masked, not clipped: a fake -90%
    # would otherwise land in one day's regression and contaminate every factor return.
    R = pd.DataFrame({t: px["Close"].pct_change() for t, px in prices.items()}).loc[START:END]
    R = R.where(R.abs() <= 0.5)
    X = exp_long.pivot_table(index=["Date", "Position"], columns="Factor", values="Loading")
    fac_rows, spec_rows, reg_rows = [], [], []
    dates = sorted(exp_long["Date"].unique())
    # Barra timing: exposures update monthly; factor returns are estimated DAILY by
    # regressing each day's stock returns on the latest *prior* month-end exposures.
    for d0, d1 in zip(dates, dates[1:] + [pd.Timestamp(END)]):
        # Missing standardized exposure = 0 (the market-average tilt; Barra convention).
        # Keep names carrying a MAJORITY of the style loadings and zero-fill the rest. Majority,
        # not a fixed count: a hard-coded "6" silently moved the pricing-coverage gate whenever
        # the factor set changed (the NonLinSize drop test unpriced young IPOs for their first
        # ~6 months because they went from 6 loadings to 5 against a threshold written for a
        # 10-style model). 11//2+1 == 6, so this is behavior-identical for the current model.
        Xd = X.loc[d0]
        Xd = Xd[Xd.notna().sum(axis=1) >= Xd.shape[1] // 2 + 1].fillna(0.0)
        # restrict to names with a daily return series (needed for fit AND residuals)
        tk_all = pd.Series({fig: fig2tkr.get(fig) for fig in Xd.index})
        Xd = Xd[tk_all.reindex(Xd.index).isin(R.columns).values]
        # the FIT cross-section is estimation names only; pick the regressor columns and the
        # degeneracy guard on that sub-cross-section (a ~constant regressor is collinear with the
        # intercept and lets lstsq answer with huge offsetting coefficients).
        em = Xd.index.isin(est_pos)
        Xe = Xd[em].loc[:, Xd[em].std() > 0.05]
        if len(Xe) < 30 or "Size" not in Xe:            # Size also needed for WLS weights
            continue
        cols = list(Xe.columns)
        figs_all = Xd.index
        tk_all = tk_all[figs_all].values
        Xs_all = np.column_stack([np.ones(len(figs_all)), Xd[cols].values])   # [1 | styles], coverage
        W = np.sqrt(np.exp(Xe["Size"].values) ** 0.5)                        # Barra: weight ~ sqrt(mktcap)
        # GICS industry dummies under the Barra constraint Σ_s c_s·f_s = 0 (c = each sector's
        # WLS weight mass in the estimation fit). Imposed by substituting the reference
        # (heaviest) sector out of the design — D̃_j = D_j − (c_j/c_ref)·D_ref — so Market
        # REMAINS the weighted-market intercept and industries are pure relative-sector
        # returns. The transformed design predicts every name correctly (a ref-sector name's
        # fitted industry return IS the recovered f_ref), so the residual line is unchanged.
        # "Unknown" sectors carry no dummy: priced by Market + styles alone.
        sects_all = np.array([fig2sect.get(f, "Unknown") for f in figs_all])
        inds = sorted({s for s in sects_all[em] if s != "Unknown"})
        D_all = (np.column_stack([(sects_all == s).astype(float) for s in inds])
                 if inds else np.zeros((len(figs_all), 0)))
        cmass = (W[:, None] ** 2 * D_all[em]).sum(axis=0) if inds else np.array([])
        ok_c = cmass > 0
        inds = [s for s, k_ in zip(inds, ok_c) if k_]
        D_all, cmass = D_all[:, ok_c], cmass[ok_c]
        if len(inds) >= 2:
            iref = int(np.argmax(cmass))
            nr = [j for j in range(len(inds)) if j != iref]
            Dt_all = D_all[:, nr] - np.outer(D_all[:, iref], cmass[nr] / cmass[iref])
        else:
            iref, nr, Dt_all = None, [], np.zeros((len(figs_all), 0))
        Xm_all = np.column_stack([Xs_all, Dt_all])                            # all coverage names
        Xm_est = Xm_all[em]                                                   # estimation subset
        days = R.index[(R.index > d0) & (R.index <= d1)]
        Rsub = R.loc[days, tk_all]
        for d in days:
            y_all = Rsub.loc[d].values
            y_est = y_all[em]
            ok_e = ~np.isnan(y_est)
            if ok_e.sum() < 30:                          # not enough estimation names to fit
                continue
            Xw = Xm_est[ok_e] * W[ok_e, None]
            yw = y_est[ok_e] * W[ok_e]
            beta, *_ = np.linalg.lstsq(Xw, yw, rcond=None)
            # fit health: weighted cross-sectional R² (about the weighted mean) + per-factor
            # t-stats from the WLS coefficient covariance — cheap, and thrown away otherwise
            wts = W[ok_e] ** 2
            ybar = float(np.sum(wts * y_est[ok_e]) / np.sum(wts))
            sst = float(np.sum(wts * (y_est[ok_e] - ybar) ** 2))
            ssr = float(np.sum(wts * (y_est[ok_e] - Xm_est[ok_e] @ beta) ** 2))
            r2 = (1.0 - ssr / sst) if sst > 0 else None
            n_, k_ = Xw.shape
            tvals = None
            if n_ > k_:
                try:
                    se = np.sqrt(np.clip(np.diag(np.linalg.inv(Xw.T @ Xw)) * ssr / (n_ - k_),
                                         0.0, None))
                    with np.errstate(divide="ignore", invalid="ignore"):
                        tvals = np.where(se > 0, beta / se, np.nan)
                except np.linalg.LinAlgError:
                    tvals = None
            names = ["Market"] + cols + [f"Ind:{inds[j]}" for j in nr]
            for j, (name, b) in enumerate(zip(names, beta)):
                fac_rows.append({"Date": d, "Factor": name, "Return": b})
                t_j = (float(tvals[j]) if tvals is not None and np.isfinite(tvals[j]) else None)
                reg_rows.append({"Date": d, "Factor": name, "TStat": t_j, "R2": r2, "N": n_})
            if iref is not None:
                # the reference sector's return is implied by the constraint, not a free
                # parameter — emitted to the scenario cache, omitted from the t-stat table
                k1 = 1 + len(cols)
                f_ref = -float(np.dot(cmass[nr], beta[k1:]) / cmass[iref])
                fac_rows.append({"Date": d, "Factor": f"Ind:{inds[iref]}", "Return": f_ref})
            # specific residual for EVERY coverage name with a return that day (estimation + held)
            resid_all = y_all - Xm_all @ beta
            ok_a = ~np.isnan(y_all)
            for fig, u in zip(figs_all[ok_a], resid_all[ok_a]):
                spec_rows.append({"Date": d, "Position": fig, "u": u, "u2": u * u})
    factor_returns = pd.DataFrame(fac_rows)
    regression_stats = pd.DataFrame(reg_rows)
    spec = pd.DataFrame(spec_rows).sort_values(["Position", "Date"])
    # 7th frame: the residual VALUE, kept daily (not just its square). Taken before the
    # month-end restamp below so Date stays the trade day.
    specific_returns = (spec[["Date", "Position", "u"]]
                        .rename(columns={"u": "SpecificReturn"}).reset_index(drop=True))
    # EWMA of squared DAILY specific returns -> daily specific variance per name
    # (transform keeps row alignment; the old reset_index/paste-Date-column approach
    #  reordered rows by Position while pasting dates in Date order -> misaligned keys)
    spec["SpecificVar"] = (spec.groupby("Position")["u2"]
                           .transform(lambda s: s.ewm(halflife=EWMA_HALFLIFE_D).mean()))
    # Snapshot at calendar month-ends: the cube joins specific_var to the monthly
    # exposure/position calendar on Date, so emit the EWMA state as of each month's
    # last trading day, stamped with the calendar month-end. Units are now DAILY
    # variance — consistent with the daily scenario VaR horizon.
    spec["Date"] = spec["Date"] + pd.offsets.MonthEnd(0)
    spec = spec.sort_values("Date").groupby(["Position", "Date"], as_index=False).last()
    return factor_returns, spec[["Date", "Position", "SpecificVar"]], specific_returns, \
        regression_stats


# --------------------------------------------------------------------------- 7. orchestrator
def _pull_manager_positions(mgr: dict) -> tuple[pd.DataFrame, dict]:
    """Pull, stitch (multi-CIK rename), and ETP-filter ONE manager's full 13F history. Returns
    (kept_df with a Book column added, per-manager stats dict for the managers.parquet frame --
    n_positions_distinct is filled in later by the caller, once the coverage-universe crosswalk +
    as-of join exist)."""
    ciks = mgr["cik"] if isinstance(mgr["cik"], tuple) else (mgr["cik"],)
    dfs = [positions_from_13f(c) for c in ciks]
    stitched = stitch_multi_cik(dfs)
    kept, etp_disc = filter_etps(stitched)
    kept = kept.copy()
    kept["Book"] = mgr["book"]
    stats = {
        "Book": mgr["book"],
        "CIK": ",".join(str(c) for c in ciks),
        "EntityName": mgr["name"],
        "FirmType": mgr["type"],
        "first_filing_date": (str(stitched["report_date"].min().date())
                              if not stitched.empty else None),
        "last_filing_date": (str(stitched["report_date"].max().date())
                             if not stitched.empty else None),
        "n_filings": int(kept["report_date"].nunique()) if not kept.empty else 0,
        "n_distinct_cusips_parsed": int(kept["cusip"].nunique()) if not kept.empty else 0,
        **etp_disc,
    }
    return kept, stats


def build_frames(out_dir=None):
    """`out_dir` (optional) redirects the ONE side-effect write this function makes directly —
    regression_stats.parquet — away from the production `DATA_DIR`. Defaults to `DATA_DIR`
    (unchanged behaviour for a real intentional build run via `__main__`). A scoped/verification
    caller that builds frames in-process (not through `__main__`) and writes the returned
    DataFrames somewhere else MUST also pass `out_dir` here, or this side-write will still land
    in the real `data/` dir regardless of where the caller sends the other eight frames — this
    bit it once (see PROJECT_HISTORY / phase notes)."""
    _out_dir = pathlib.Path(out_dir) if out_dir is not None else DATA_DIR
    cal = pd.date_range(START, END, freq="ME")          # monthly leaf calendar

    # --- positions + identity resolution -----------------------------------
    managers = [m for m in MANAGERS if (ACTIVE_MANAGERS is None or m["book"] in ACTIVE_MANAGERS)]
    if not managers:
        raise ValueError(f"ACTIVE_MANAGERS={ACTIVE_MANAGERS!r} matched none of MANAGERS")
    print(f"managers: {[m['book'] for m in managers]}", flush=True)
    pos_frames, mgr_stats = [], []
    for mgr in managers:
        print(f"  pulling 13F: {mgr['book']} (CIK {mgr['cik']}) ...", flush=True)
        kept, stats = _pull_manager_positions(mgr)
        pos_frames.append(kept)
        mgr_stats.append(stats)
        print(f"  {mgr['book']}: {stats['n_filings']} filings, "
              f"{stats['n_distinct_cusips_parsed']} distinct cusips parsed, "
              f"ETP drop {stats['n_dropped_latest']} rows / "
              f"{stats['dropped_value_share_latest']:.1%} of latest filing value", flush=True)
    pos13f = (pd.concat(pos_frames, ignore_index=True) if pos_frames else
             pd.DataFrame(columns=["report_date", "filing_date", "issuer", "cusip", "value",
                                   "shares", "sshType", "putCall", "Book"]))
    xw = crosswalk_cusips(pos13f["cusip"].tolist())
    t2c = ticker_to_cik()
    # The estimation universe = all 13F-held names (the book's opportunity set, kept whole so the
    # book is always a subset) UNION a market-index seed (SEED_INDEX / UNIVERSE_EXTRA). This makes
    # the cross-section a real market rather than the manager's holdings alone; no alphabetical
    # truncation. UNIVERSE_CAP is now only a safety bound. Index names are keyed by ticker (no FIGI)
    # and any already present as a held name are dropped to avoid double-counting one company.
    # sec is now built from the UNION of every book's held CUSIPs (pos13f spans all managers), so the
    # coverage universe automatically extends across managers -- is_estimation below still marks
    # ONLY the S&P 500 seed, independent of which manager(s) hold a name.
    sec = (xw.dropna(subset=["figi", "ticker"])
             .merge(t2c, on="ticker", how="left")
             .dropna(subset=["cik"]).drop_duplicates("figi"))
    extra_tickers = list(dict.fromkeys([t.lower() for t in UNIVERSE_EXTRA] + index_constituents()))
    extra = (pd.DataFrame({"ticker": extra_tickers})
               .merge(t2c, on="ticker", how="left").dropna(subset=["cik"]))
    extra = extra[~extra["ticker"].isin(sec["ticker"])].copy()   # don't double-count held names
    if not extra.empty:
        extra["figi"] = extra["ticker"]; extra["cusip"] = None
        sec = pd.concat([sec, extra], ignore_index=True).drop_duplicates("figi")
    sec = sec.head(UNIVERSE_CAP).reset_index(drop=True)
    sec["cik"] = sec["cik"].astype(int)
    # Estimation/coverage split: the ESTIMATION universe is the clean market-index seed (the S&P 500),
    # the names the factor returns are fit on and whose loadings are winsorized. COVERAGE = estimation
    # ∪ every held name; coverage-only names (held but not in the seed) get UNCAPPED loadings + their
    # own specific risk, but never enter the factor-return regression. With the split off (or no seed)
    # every name is estimation, reproducing the legacy single-universe behaviour.
    seed_set = set(index_constituents()) if (SEED_INDEX and UNCAP_COVERAGE) else set()
    sec["is_estimation"] = sec["ticker"].isin(seed_set) if seed_set else True
    n_est = int(sec["is_estimation"].sum())
    print(f"universe: {len(sec)} coverage names; estimation (seed ∩ data) = {n_est}, "
          f"coverage-only (uncapped) = {len(sec) - n_est}", flush=True)

    # --- raw market + fundamentals pulls (parallel, cached, with progress) --
    print(f"data pull: {len(sec)} names ({_HTTP['hit']} cache hits so far)", flush=True)
    mkt = stooq_daily(MARKET_PROXY)
    rates = stooq_daily(RATE_PROXY)
    ndx = stooq_daily(NDX_PROXY)
    prices = {t: p for t, p in _pull_map(stooq_daily, sec["ticker"], "prices").items()
              if p is not None}
    funda  = {int(c): f for c, f in
              _pull_map(fundamentals, [int(c) for c in sec["cik"].unique()], "fundamentals").items()}

    # --- exposures (leaf) + factor cache + specific risk -------------------
    exposures = build_exposures(sec, prices, funda, cal, mkt, rates, ndx)
    # GICS sector joined BEFORE the regression (2026-07-04): the industry block needs it as
    # dummy membership; the securities dimension reuses the same column below (one fetch).
    gics = sectors_for_ciks(sec["cik"])
    sec = sec.merge(gics[["CIK", "Sector"]].rename(columns={"CIK": "cik"}), on="cik", how="left")
    sec["Sector"] = sec["Sector"].fillna("Unknown")
    factor_returns, specific_var, specific_returns, reg_stats = \
        regress_factors(exposures, prices, sec)
    # regression-health side artifact (daily weighted R² + per-factor t-stats). Written here,
    # not in __main__, so it exists however build_frames is invoked; the cube never reads it —
    # only /regression does. Not part of the seven-frame contract.
    _out_dir.mkdir(parents=True, exist_ok=True)
    reg_stats.to_parquet(_out_dir / "regression_stats.parquet", index=False)
    # Add Market as a LEAF loading of 1.0 for every (Date, Position). In v2 Market is the
    # cross-sectional regression intercept, so each name loads exactly 1.0 on it and a
    # fully-invested book (weights sum to 1) has unit market exposure. Done AFTER regress_factors
    # (which must see style exposures only) so directional market risk flows through the scenario
    # engine instead of being dropped. x_Market = Σ weights; Market shock = the intercept series.
    mkt_load = (exposures[["Date", "Position"]].drop_duplicates()
                .assign(Factor="Market", Loading=1.0))
    exposures = pd.concat([exposures, mkt_load], ignore_index=True)
    # Industry LEAF loadings (2026-07-04): each name loads 1.0 on its GICS sector — the dual of
    # the regression dummies — so sector P&L flows through the scenario engine and the cube can
    # drill factor risk by industry. Restricted to industries that actually earned a factor
    # return (all 11 in practice); "Unknown" sectors carry no industry loading.
    ind_facs = sorted({f for f in factor_returns["Factor"].unique() if str(f).startswith("Ind:")})
    have_ind = {f[4:] for f in ind_facs}
    sect_map = sec.set_index("figi")["Sector"].to_dict()
    ind_load = (exposures.loc[exposures["Factor"] == "Market", ["Date", "Position"]]
                .assign(Sector=lambda x: x["Position"].map(sect_map)))
    ind_load = ind_load[ind_load["Sector"].isin(have_ind)]
    ind_load = (ind_load.assign(Factor="Ind:" + ind_load["Sector"], Loading=1.0)
                [["Date", "Position", "Factor", "Loading"]])
    exposures = pd.concat([exposures, ind_load], ignore_index=True)

    # --- per-book weights, as-of joined onto the monthly calendar ----------
    # Each calendar date carries exactly the latest filing's book: the as-of join picks the filing,
    # then an inner join takes only names in *that* filing — positions exited in a newer filing
    # expire instead of persisting forever. Weight is normalised PER (Book, filing_date) so each
    # manager's own filing sums to 1.0 independent of every other manager. The as-of join is done
    # PER BOOK (a plain loop, not merge_asof(..., by="Book")) because each manager has its own filing
    # calendar -- a single global calendar (the old one-shot merge_asof) would silently assign one
    # manager's filing dates to another once there's more than one book.
    p = pos13f.merge(sec[["cusip", "figi"]], on="cusip", how="inner")
    p = (p.groupby(["Book", "filing_date", "figi"], as_index=False)["value"].sum()
           .rename(columns={"figi": "Position", "value": "MV"}))   # collapse multi-lot/multi-CUSIP rows
    p["Weight"] = p.groupby(["Book", "filing_date"])["MV"].transform(lambda v: v / v.sum())
    pos_parts = []
    for book, pb in p.groupby("Book"):
        filings_b = pd.DataFrame({"filing_date": np.sort(pb["filing_date"].unique())})
        cal_b = pd.merge_asof(pd.DataFrame({"Date": cal}), filings_b,
                              left_on="Date", right_on="filing_date", direction="backward")
        pos_parts.append(cal_b.dropna(subset=["filing_date"]).merge(pb, on="filing_date"))
    positions = (pd.concat(pos_parts, ignore_index=True) if pos_parts else
                pd.DataFrame(columns=["Date", "filing_date", "Book", "Position", "MV", "Weight"]))
    # --- ADV (avg daily $ volume) for days-to-liquidate (Step 11) ----------
    # Per (Date, Position): trailing-63-day mean dollar volume from the cached prices, sampled at each
    # month-end. Carried on the positions frame (held names only) so the API can read days-to-liquidate
    # = MV / (participation * ADV) without re-pulling. NaN where price/volume is missing.
    fig2tkr = dict(zip(sec["figi"], sec["ticker"]))
    adv_rows = []
    for P in positions["Position"].unique():
        px = prices.get(fig2tkr.get(P))
        if px is None or "Volume" not in px:
            continue
        dv = (px["Close"] * px["Volume"]).rolling(63, min_periods=20).mean()
        idx = px.index.searchsorted(cal, side="right")
        for D, i in zip(cal, idx):
            if i > 0 and pd.notna(dv.iloc[i - 1]):
                adv_rows.append({"Date": D, "Position": P, "ADV": float(dv.iloc[i - 1])})
    adv = pd.DataFrame(adv_rows) if adv_rows else pd.DataFrame(columns=["Date", "Position", "ADV"])
    positions = positions.merge(adv, on=["Date", "Position"], how="left")
    positions = positions[["Date", "Book", "Position", "Weight", "MV", "ADV"]]

    # --- dimensions --------------------------------------------------------
    securities = sec.rename(columns={"figi": "Position", "ticker": "Ticker",
                                     "cusip": "CUSIP", "title": "Issuer"})
    securities = securities[["Position", "Ticker", "cik", "CUSIP", "Issuer", "Sector"]] \
        .rename(columns={"cik": "CIK"})
    # GICS sector already on sec (joined before regress_factors for the industry block)
    # Country of incorporation from the same SEC submissions JSON (US state -> 'US', foreign -> country);
    # names with no CIK keep the US default. See countries_for_ciks. ~21% of the Soros book by weight is
    # genuinely non-US (Alibaba/JD China, Canadian energy/industrials, Sanofi), so this is not cosmetic.
    ctry = countries_for_ciks(securities["CIK"])
    securities = securities.merge(ctry[["CIK", "Country"]], on="CIK", how="left")
    securities["Country"] = securities["Country"].fillna("US")
    factor_meta = pd.DataFrame({"Factor": ["Market"] + STYLE_FACTORS + ind_facs,
                                "FactorGroup": (["Market"] + ["Style"] * len(STYLE_FACTORS)
                                                + ["Industry"] * len(ind_facs))})

    # --- 8th (optional) frame: managers.parquet -----------------------------
    # One row per Book: CIK(s) used, EDGAR entity name, firm type/strategy, first/last filing date
    # actually parsed, filing count, distinct-position count (POST crosswalk, i.e. what actually made
    # it into `positions` -- a more meaningful number than the raw pre-crosswalk CUSIP count also kept
    # here), and the Task C ETP-drop disclosure. Optional by contract like specific_returns: nothing
    # else in this builder reads it back, so its absence never breaks anything downstream.
    managers_df = pd.DataFrame(mgr_stats)
    n_pos_by_book = positions.groupby("Book")["Position"].nunique().rename("n_positions_distinct")
    managers_df = managers_df.merge(n_pos_by_book, left_on="Book", right_index=True, how="left")
    managers_df["n_positions_distinct"] = managers_df["n_positions_distinct"].fillna(0).astype(int)

    return (exposures, positions, securities, factor_meta, factor_returns, specific_var,
           specific_returns, managers_df)


if __name__ == "__main__":
    out = DATA_DIR
    frames = build_frames(out_dir=out)
    names = ["exposures", "positions", "securities", "factor_meta", "factor_returns", "specific_var",
             "specific_returns", "managers"]
    out.mkdir(exist_ok=True)
    for nm, df in zip(names, frames):
        df.to_parquet(out / f"{nm}.parquet", index=False)
        print(f"{nm:14s} {df.shape}")
    print(f"\nHTTP this run: {_HTTP['miss']} network fetches, {_HTTP['hit']} cache hits "
          f"-> all responses cached under {CACHE_DIR} (rerun re-pulls nothing).")
    print("\nFeed build_frames() (or the parquet files) straight into build_cube() from the cube scaffold.")
