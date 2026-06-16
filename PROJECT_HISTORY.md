# Project history & background

Reference doc for how this POC got to its current state.

> **TODO:** the original design conversation lives at
> https://claude.ai/share/7810ac6f-0859-41ca-bc1f-b3355aa71d4f — it could not be fetched
> headlessly (claude.ai share pages are JS-rendered behind a Cloudflare challenge).
> Paste its transcript here (or below under "Original design discussion") for the full story.

## What this is

A proof-of-concept Barra-style equity factor-risk model built entirely on free/public data,
demoed against the Soros Fund Management 13F book (CIK 0001029160). Two halves communicating
only through six parquet frames in `/mnt/user-data/outputs/`:

1. **Frame builders** (`barra_build_frames.py` v2 canonical, `barra_build_frames_v1.py`
   alternative) pull SEC EDGAR 13F/XBRL, OpenFIGI, and daily prices, and emit
   `exposures`, `positions`, `securities`, `factor_meta`, `factor_returns`, `specific_var`.
2. **The Atoti cube** (`barra_factor_risk_cube.py`) consumes the six frames and exposes
   exposures plus a unified scenario engine (historical sim / event replay / hypothetical
   shocks — all the same operation `dPnL = Σ_k x_k · df_k` with different shock-vector sources).

See `CLAUDE.md` for the architecture contract (frame schemas, v1-vs-v2 differences,
the FIGI identity key, the partial-join scenario design, and known sharp edges).

## Original design discussion

*(placeholder — paste the shared claude.ai conversation here)*

## Working session — 2026-06-12 (environment bring-up & first successful build)

Chronological record of getting the pipeline from "scripts in a folder" to six valid frames.

### Environment

- Created `requirements.txt` (direct deps) by auditing imports: `numpy`, `pandas`,
  `requests`, `pyarrow` (parquet backend), `atoti`.
- Created venv **`barra/`** (Python 3.12.3); installed and then **pinned** resolved versions:
  `numpy==2.4.6`, `pandas==2.3.3`, `requests==2.34.2`, `pyarrow==24.0.0`, `atoti==0.9.15`
  (atoti bundles its own JDK via `jdk4py`, no system Java needed).
- Generated **`requirements.lock`** from `pip freeze` (full transitive graph, 31 packages).
- `py-spy` was installed into the venv as a debugging aid (not a project dep).
- Created `/mnt/user-data/outputs/` (required sudo; chowned to user) — the hardcoded
  builder↔cube handoff directory.
- Set `SEC_UA` in `barra_build_frames.py` to a real contact (SEC blocks anonymous traffic).

### Failure 1 — Stooq is blocked from this machine

First build run crashed: `TypeError: 'NoneType' object is not subscriptable` on the market
proxy. Root cause: **Stooq serves a JavaScript anti-bot challenge page** (HTTP 200, HTML)
to this host for every request — not header-fixable, confirmed with browser User-Agents.
Worse, `_get()` cached the 200-status challenge pages, **poisoning the disk cache** (251
entries, zero valid price CSVs; every `stooq_daily` silently returned `None`).

**Fix:**
- Purged the poisoned cache entries (valid SEC/OpenFIGI responses kept).
- Added `_yahoo_daily()` — Yahoo Finance chart API fallback, same return contract
  (`Date` index, `Close` = adjusted close, `Volume`).
- `stooq_daily()` now detects non-CSV (HTML-leading) responses and falls through to Yahoo.
  Since v1 imports `stooq_daily` from v2, both builders are covered.

### Failure 2 — missing XBRL tags

Next run died with `KeyError: 'Shares'`: `fundamentals()` only guaranteed the `Equity`
column; companies that never reported a tag (e.g. `CommonStockSharesOutstanding`) produced
frames missing that column, and `build_exposures` indexes `fa["Shares"]` / `NetIncome` /
`Assets` unconditionally.

**Fix:** `fundamentals()` now always emits all five `_TAGS` columns, NaN-filling
never-reported ones (downstream winsorize/z-score tolerates NaN).

### Failure 3 — self-inflicted timeout

A rerun died silently with no traceback: it had been launched as a background task with a
10-minute harness timeout and was SIGKILLed mid-price-phase. Relaunched detached via
`nohup` → completed in ~2 minutes on the warm cache.

### Performance / ergonomics changes

- **OpenFIGI POST caching:** `crosswalk_cusips` previously re-hit the OpenFIGI mapping API
  live on every run (4–8 min at the unauthenticated 25/min limit) because POSTs bypassed
  the GET-only cache. Added `_post_json()` — same disk cache, keyed on url + sorted JSON
  body; rate-limit sleep only on cache miss. Crosswalk is now free after the first run.
- **`OPENFIGI_KEY` from environment:** the constant now reads
  `os.environ.get("OPENFIGI_KEY") or None` (key obtained from
  https://www.openfigi.com/user/profile/api-key, exported in `~/.bashrc`). With a key:
  100-CUSIP batches at 0.3 s vs 10 at 2.6 s.

### Correctness fix — positions overlay was accumulating exited names

The original per-position backward `merge_asof` carried every 13F position forward
**forever** — names exited in later filings kept their last filed weight, so per-date
weights summed to ~4.96 by end of sample instead of 1.0.

**Fix (in `build_frames`):** as-of join now selects the *filing* for each calendar date,
then an inner join takes only names in that filing — exits expire on the next filing.
Also collapsed multi-lot/multi-CUSIP rows per (filing, FIGI) by summing value (the old
code silently kept one arbitrary row). Result: weights sum to exactly 1.0 on all 108
dates; 12–32 names held per date; `positions` shrank 15,385 → 2,148 rows. Schema unchanged.

### Final state — frames built & verified

| Frame | Shape | Notes |
|---|---|---|
| `exposures` | 177,848 × 4 | 2016-01 → 2024-12, 10 style factors, 248 names, 0% null loadings |
| `positions` | 2,148 × 5 | Book=Soros, point-in-time correct, weights sum to 1.0/date |
| `securities` | 250 × 7 | Sector/Country still stubbed ("Unknown"/"US") |
| `factor_meta` | 11 × 2 | Market + 10 style |
| `factor_returns` | 803 × 3 | 2016-05 → 2024-12, incl. Market intercept |
| `specific_var` | 4,417 × 3 | all ≥ 0 |

### Known open items

- The Atoti cube (`barra_factor_risk_cube.py`) has **not yet been run** against these
  frames; CLAUDE.md warns the `tt.array.*` / `tt.agg.sum_product` signatures may differ
  in atoti 0.9.15 — verify before assuming.
- Pandas `FutureWarning` on `pct_change` default fill (line ~315) — harmless today,
  breaks on a future pandas major.
- Sector/Country enrichment (GICS/SIC map) still pending.
- Stooq remains blocked from this host; prices come via the Yahoo fallback. Stooq
  challenge pages are cached (they short-circuit straight to Yahoo); delete
  `~/.cache/barra_poc` to retry Stooq fresh.

### Operational notes

- HTTP cache: repo-local `tmp/` dir, gitignored (md5-of-URL keyed; POSTs keyed on url+body);
  originally `~/.cache/barra_poc`, moved 2026-06-12. Warm-cache full rebuild ≈ 1–2 min.
- Scripts moved into `python_src/` on 2026-06-12.
  Run: `cd python_src && ../barra/bin/python barra_build_frames.py`.
- `SEC_UA` must stay a real "name email" string; `OPENFIGI_KEY` comes from the env.
