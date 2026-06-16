# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A proof-of-concept Barra-style equity factor-risk model built entirely on free/public data,
demoed against the Soros Fund Management 13F book. It splits cleanly into two halves:

1. **Frame builders** pull raw data and emit six canonical parquet frames.
2. **The Atoti cube** consumes those six frames and exposes exposures + a unified
   scenario/stress engine (historical sim, event replay, hypothetical shocks).

The two halves communicate *only* through the six parquet files written to the repo-local
`data/` dir (gitignored). Run a builder, then run the cube — they share no in-process state.

## Running

Scripts live in `python_src/`; the Python environment is the `barra/` venv at the repo root
(deps pinned in `requirements.txt`, full freeze in `requirements.lock`). There is no build
system or test suite — scripts are run directly:

```bash
cd python_src
../barra/bin/python barra_build_frames.py        # v2 (default): characteristic z-scores + own regression
../barra/bin/python barra_build_frames_v1.py     # v1 (alternative): returns-based time-series betas
../barra/bin/python barra_factor_risk_cube.py    # builds the cube; expects the six parquets to already exist
```

**Before running a builder:** `SEC_UA` in `python_src/barra_build_frames.py:43` must be a real
`"name email"` string — SEC EDGAR blocks anonymous traffic and the pipeline will fail without it.
`OPENFIGI_KEY` is read from the environment (optional; raises OpenFIGI batch/rate limits).

Both builders write the six frames to `data/` at the repo root (created on demand) and the
cube reads from there (`OUT` / `out` constants, resolved relative to the script file).

## The two model versions (v1 vs v2)

Both produce the **identical six-frame schema** and feed the same unchanged cube. They differ
only in how `exposures`, `factor_returns`, and `specific_var` are computed:

- **v2 — `barra_build_frames.py`** (the primary/canonical builder). Exposures are
  cross-sectional characteristic **z-scores** (Size, Value, Momentum, etc.). Factor returns
  and specific risk are **derived** from a monthly cross-sectional WLS regression of forward
  returns on lagged exposures — so exposures, factor returns, and specific risk are duals of
  one regression. Monthly calendar, sample 2016–2024.

- **v1 — `barra_build_frames_v1.py`** (alternative). Exposures are per-name **time-series
  betas** regressed against *published* daily factor returns (JKP clusters, else Ken French
  FF5+Mom). Factor returns are the downloaded series used **verbatim** (never regressed).
  Specific var is EWMA of regression residuals. Sample 2022–2024.

v1 **imports plumbing from v2** (`positions_from_13f`, `crosswalk_cusips`, `ticker_to_cik`,
`stooq_daily`, `_get`, and config constants). So `barra_build_frames.py` is the shared library;
editing its data-fetch functions or constants affects both builders.

## Data flow and external sources

All sources are free/public, fetched over HTTP with a polite disk cache:

- **positions** → SEC EDGAR 13F (Soros, CIK 0001029160) — the book held as a *weight overlay*.
- **crosswalk** → OpenFIGI v3 (CUSIP→FIGI/ticker) + SEC `company_tickers.json` (ticker→CIK).
- **fundamentals** → SEC EDGAR XBRL company-facts API (point-in-time, CIK-keyed).
- **prices/returns** → Stooq per-symbol daily CSV (ticker-keyed), with a **Yahoo chart-API
  fallback** (`_yahoo_daily`, same Date/Close/Volume contract) when Stooq serves its JS
  anti-bot challenge page instead of CSV — which it does for all traffic from some hosts.
- **factor returns (v1 only)** → JKP daily clusters or Ken French daily files.

`_get()` (in `barra_build_frames.py`) disk-caches every HTTP response under the repo-local
`tmp/` dir (gitignored), keyed by URL md5; `_post_json()` does the same for POSTs (OpenFIGI),
keyed on url + body. Reruns
are cheap and don't re-hit rate-limited APIs; a warm-cache full rebuild takes ~1–2 min. Delete
that cache dir to force a fresh pull.

### The canonical identity key

**`Position` == `SecId` == FIGI**, resolved up front. Every frame keys positions on FIGI so the
cube only ever joins on `Position`. CUSIP/ticker/CIK exist only inside the builders to bridge the
different source APIs; they are resolved to a single FIGI before frames are emitted.

## The six frames (contract between builders and cube)

| Frame | Key | Payload | Role |
|---|---|---|---|
| `exposures` | (Date, Position, Factor) | Loading | the granular leaf |
| `positions` | (Date, Book, Position) | Weight, MV | Soros 13F weight overlay, as-of joined |
| `securities` | (Position) | Ticker, CIK, CUSIP, Issuer, Sector, Country | dimension |
| `factor_meta` | (Factor) | FactorGroup | dimension |
| `factor_returns` | (Date, Factor) | Return | the shared scenario cache |
| `specific_var` | (Date, Position) | SpecificVar | diagonal idiosyncratic block |

Two risk blocks only: a linear **factor P&L** block (driven by `factor_returns`) and a
**diagonal specific-risk** block (`specific_var`). No full specific covariance matrix.

The 13F book is a quarterly weight overlay **as-of joined** onto the monthly/COB calendar
(lagged by filing date via `pd.merge_asof(..., direction="backward")`). The as-of join selects
the latest *filing* per calendar date and takes only the names in that filing — exited positions
expire on the next filing, so weights sum to 1.0 on every date.

## The cube's central design: scenarios as one operation

`barra_factor_risk_cube.py` is built around a single idea — **all three scenario modes are the
same operation** `dPnL = Σ_k x_k · df_k`, differing only in the *source* of the shock vector:

- `HistFull` → full factor-return history (historical-simulation VaR/ES)
- `Evt:*` → factor returns over a past window (event replay; see `EVENT_WINDOWS`)
- `Hypo:*` → hand-set sigma-shocks, length-1 vectors (see `HYPO_SHOCKS`)

The mechanism is a **partial Atoti join**: `t_exp.join(t_scn, on Factor only)` makes
`ScenarioSet` a *hierarchy* rather than a join key. Slicing that hierarchy selects which
`ShockVec` flows through the same `Scenario PnL vector` measure — that slice *is* "the switch."
`OriginScope({l["Factor"]})` pins shock-vector scaling to one vector per factor.

**Critical constraint:** vector lengths differ across scenario sets (full history vs a 3-month
window vs length-1). Always query the scenario risk measures (`Scenario VaR 99`,
`Scenario worst loss`, etc.) **sliced to a single `ScenarioSet`** — mixing sets in one cell
compares ragged vectors. The `Market` factor **is included** in scenarios: it carries a leaf
loading of 1.0 per name (the v2 cross-sectional regression intercept), so a fully-invested book
has unit market exposure (`x_Market = Σ weights`) and the directional market return flows through
`dPnL`. This is what makes `Scenario VaR 99` / `Total VaR 99` read as real long-equity book risk
(~3.5% daily 99% VaR) rather than style-tilt-only (~1.5%). Market loadings are added in
`build_frames` *after* `regress_factors` so the style factor returns are unaffected.

## Things that bite

- **`atoti` array helper names** (`tt.array.mean/quantile/min`, `tt.agg.sum_product` with
  `scope=`) may vary by SDK version — the source flags this. Verify against the installed
  `atoti` before assuming a call signature.
- **Universe is capped** at `UNIVERSE_CAP = 250` so the demo actually runs; factor-return
  quality scales with cross-section breadth, so widen `UNIVERSE_EXTRA` / the cap for anything
  real. v2's regression skips dates with `< 30` valid names.
- **Sector** is populated from a free SEC SIC → GICS-11 crosswalk, CIK-keyed
  (`sic_to_gics` / `sectors_for_ciks` in `barra_build_frames.py`; SIC comes from the SEC
  submissions JSON). ~5 names (BDCs/closed-end funds) carry a blank SEC SIC and fall back to
  `"Unknown"`. **Country is still stubbed** (`"US"`) in `securities`.
- Rate limits are real: SEC ≤10 req/s (handled via `sleep` in `_get`), OpenFIGI 25/min
  unauthenticated (export `OPENFIGI_KEY` to raise it; POSTs are disk-cached so the
  crosswalk is only slow on a cold cache).
- See `PROJECT_HISTORY.md` for the session log of how the pipeline was brought up
  (Stooq block, XBRL tag gaps, positions-overlay fix).
