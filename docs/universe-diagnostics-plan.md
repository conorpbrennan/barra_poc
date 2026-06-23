# Plan — universe diagnostics (bitemporal index membership → funnel → span check)

Status: Phases 1–2 BUILT (Phase 1: barra_universe_membership.py, GET /universe; Phase 2:
barra_universe_funnel.py, GET /funnel, universe_filters.json, test_funnel.py — both in the
"🌐 Estimation universe" panel); Phase 3 planned, Phase 4 awaiting Chris. Diagnostic only — no change
to the six-frame contract, the cube, or how
`barra_build_frames.py` produces loadings. This layers an analysis + a dashboard panel on top of
what already exists, so we can *see* what an estimation/coverage split would do before committing to
the builder change in `estimation-coverage-design.md`.

Companion to `estimation-coverage-design.md` (that note proposes the builder split; this note is the
diagnostic that justifies it and answers Chris's question on the data).

## What this answers

Two commitments made to Chris, plus the visualization idea:

1. **"I'll check which indices Soros holdings are in for each filing."** → Phase 1. Bitemporal.
2. **"Apply the filtering you suggest and detail which underlyings are excluded and why, so we can
   see if the DQ is performing as expected."** → Phase 2 (the funnel).
3. **"Does the book sit inside the estimation universe's span → high confidence?"** (Chris's
   VALUE/SIZE picture) → Phase 3 (the span check).

The headline Phase 1 produces is the one Chris flagged as decisive: *what fraction of the Soros book,
by weight, sits in names not covered by S&P 1500?* If it's ~0, the SP1500-vs-R3000 choice doesn't
matter and we estimate on SP1500. If it's material, Russell 3000 earns its extra data cost.

---

## Phase 1 — bitemporal index-membership analysis (build first)

### The data we already have (holdings side — fully bitemporal)

Every 13F row from `positions_from_13f(SOROS_CIK)` (`barra_build_frames.py:270`) carries both time
axes already:

- **`report_date`** (quarter-end) — *valid time*: when Soros actually held the name.
- **`filing_date`** — *transaction/knowledge time*: when it became public (13F lands ≤45 days after
  quarter-end).
- **`cusip`** + crosswalked **`ticker`** (OpenFIGI) — identity.

So the analysis must read holdings from `positions_from_13f` **raw**, not from the `positions` frame.
The frame's `Date` is the as-of-joined COB calendar; the raw parse keeps `report_date`/`filing_date`,
which is what bitemporal classification needs. SOROS CIK = `0001029160` (per CLAUDE.md).

### The data we add (membership side — two tracks, per the free-data reality)

All fetched over HTTP through the existing `_get` disk cache (keyed by URL md5), same as every other
source. Nothing live at request time.

**Track A — S&P 500, true bitemporal.**
Source: `fja05680/sp500` "S&P 500 Historical Components & Changes.csv" — survivorship-bias-free,
effective dates from 1996. Parse into per-ticker membership intervals `[start, end)`. A name is "in
S&P 500 at `report_date`" iff `report_date` falls in one of its intervals. This is genuine
point-in-time: the list as it stood then, including names later removed.

**Track B — S&P 1500 and Russell, current-membership proxy.**
Source: iShares ETF holdings CSVs — IVV (S&P 500), IJH (S&P 400), IJR (S&P 600), IWB (Russell 1000),
IWM (Russell 2000), IWV (Russell 3000). Each is a flat ticker list as-of today. Derived sets:
`S&P 1500 = IVV ∪ IJH ∪ IJR`; Russell 3000 = IWV. **Mono-temporal** — applied to a name regardless of
filing date, and itself survivorship-biased (today's index can't contain names that were delisted).
Every Track-B label is rendered with an explicit "as-of today, not PIT" tag so it's never confused
with Track A.

> Why the split: free PIT history exists cleanly for the S&P 500 only. S&P 400/600 have no clean free
> PIT set; Russell PIT is paid (Norgate/Algoseek/FTSE). Track B is the honest best-effort for "is this
> name broadly an index name or a genuine outsider," which is enough to size Chris's question.

### Identity matching

Index lists are ticker-keyed; we hold current `ticker` (OpenFIGI composite) + `cusip`. Match on
ticker, fall back to nothing (unmatched → "outside all"). Two documented caveats: (1) ticker drift —
a name's historical ticker may differ from today's OpenFIGI ticker, a known wrinkle we accept for a
POC; (2) Track B can't see delisted names. Both surfaced in the panel's notes, not hidden.

### Classification + bucketing

Per (filing, held name): flags `{in_sp500_pit, in_sp1500_now, in_r3000_now}` and a derived
mutually-exclusive bucket for a clean stacked chart:

1. **S&P 500** (PIT) — the clean core.
2. **S&P 400/600** (current) — mid/small but still S&P 1500.
3. **Russell-only** (current) — in R3000 but not S&P 1500.
4. **Outside all** — in none of the above. The coverage-beyond-estimation set.

### Aggregation + artifact

Aggregate **by 13F weight** (value ÷ filing total), not just name count — a 0.1% outsider is noise,
an 8% outsider is the story. Emit a tidy artifact `data/universe_membership.parquet`:

- per-`(report_date, filing_date, bucket)`: `weight`, `n_names`
- a name-level detail table: `(report_date, issuer, ticker, weight, bucket, flags…)` for the
  per-filing drill-down.

Built by a standalone script `python_src/barra_universe_membership.py` (CLI like
`barra_dq_checks.py`: `run()` builds + writes the parquet and prints a summary). Pure classification
logic (interval lookup, bucketing, weight aggregation) split into importable functions for unit tests.

### API — `GET /universe`

New endpoint in `risk_api.py`, mirroring `/dq` (`risk_api.py:667`): `@app.get`, `run_in_threadpool`,
returns plain dicts. Reads the precomputed `data/universe_membership.parquet` (fast — no network at
request time, no cube dependency). Returns:

- `series`: per-filing weight-by-bucket time series (for the chart),
- `latest`: the latest filing's bucket split + headline `% outside S&P 1500`,
- `detail`: the latest filing's outside-SP1500 names (issuer, weight, why),
- `notes`: the Track-A/Track-B + caveat strings.

Optional `?date=` to pick a filing other than latest.

### UI — `render_universe`

New panel in `risk_pivot_app.py`, same shape as `render_trends`/`render_drawdown`
(`risk_pivot_app.py:475`/`:438`):

- **Headline:** "X% of the latest book weight sits outside S&P 1500 (Y% outside Russell 3000)."
- **Stacked area/bar** over filings: book weight by the four buckets, 2016→latest.
- **Per-filing expander:** the names outside S&P 1500 — issuer, weight, and which (if any) broader
  index caught them — i.e. *which underlyings are excluded from an SP1500 estimation universe and
  why*, the Phase-1 form of the "detail which underlyings are excluded" commitment.
- **Notes block:** Track A (PIT) vs Track B (current proxy), survivorship + ticker-drift caveats,
  source links. This is the "document all criteria" requirement for the membership step.

### Tests — `test_universe.py`

Unit (no network): interval-membership lookup (in/out at boundary dates), mutually-exclusive
bucketing, weight aggregation sums to 1.0 per filing, the "outside SP1500" derivation. Integration
(cache-backed, opt-in like the other suites): the script builds the parquet and `/universe` serves a
well-formed payload.

---

## Phase 2 — the monthly filtration funnel (SPEC)

The funnel Chris described: a **point-in-time input population** each month → a stack of documented
data-quality filters → the **survivors** that would form the estimation universe, showing exactly
which names each filter drops and why. This is the "apply the filtering you suggest and detail which
underlyings are excluded so we can see if the DQ is performing as expected" commitment, made visible.

### Pre-filter population — LOCKED to PIT S&P 500

The objection that kills the first draft: *we can't get S&P 1500 membership point-in-time on free
data, so we can't use "in the S&P 1500" as the monthly universe definition without backward-looking
survivorship bias.* Correct. So the input population is the only index we **can** take point-in-time
on free data — the S&P 500:

> **population(t) = S&P 500 constituents as-of month-end *t*** — the hanshof change-log snapshot
> effective at *t* (Phase 1's Track-A source), which carries names that have since left or delisted, so
> it is **survivorship-free**.

Index component = **S&P 500**, by construction. The held book is **not** part of the estimation
population — it joins on the *coverage* side (estimation-survivors ∪ held, uncapped; see the design
note). Membership is then the **PIT rule-set applied to population(t)**: size, liquidity, history,
listing — all evaluable point-in-time (fundamentals as-of the SEC `filed` date, prices/volume daily).
So both the population *and* the membership are genuinely bitemporal. An index is never the gate beyond
this clean starting set — only a label.

**The honest consequence (state it up front).** The S&P 500 is already a committee-curated clean set,
so the size/liquidity/history filters drop **almost nothing** — the funnel is nearly flat by design.
That is the correct, expected result, not a bug: it shows the DQ layer agreeing that an already-clean
input is clean. The filters only *bite* on a broad, raw input (R3000-style) where illiquid micro-caps
need screening — and that input can't be had point-in-time on free data. So the funnel is framed as
**the sanity layer that confirms the input is clean and would do the heavy lifting on a broader
universe**, with the per-stage drop counts proving each filter is wired and evaluated, not dormant.
(If, later, a paid PIT source for a broader universe is available, only `population(t)` changes — the
whole funnel machinery is reused unchanged.)

### All of Chris's filters, with honest free-data availability

Each filter is a funnel stage. Most already run silently in today's pipeline — Phase 2 makes them
legible — and the data each needs is mostly already cached:

| Filter (Chris's list) | Metric | PIT? | Free-data status |
|---|---|---|---|
| **Size** — min market cap | `close × Shares` (XBRL) | ✓ | **buildable now** — `MCAP_FLOOR=$10M` exists in `build_exposures`; raise to a real bar |
| **Liquidity** — min ADV / volume | trailing dollar-volume | ✓ | **buildable now** — Volume already in cached price pulls (`price_descriptors` `dvol`); add a gate, **no new pull** |
| **Liquidity** — min trading frequency | % non-zero-volume days, trailing window | ✓ | **buildable now** — from the same cached Volume |
| **Liquidity** — min free float | float-adjusted shares | ✓ | **✗ not free** — no float source; weakly proxied by mcap. Stage shown as *unavailable*, not faked |
| **History** — min trading history | length of daily series as-of t | ✓ | **buildable now** — already gates descriptors (≥120 d / ≥252 d) |
| **History / listing** — primary common only | drop warrants, preferred, units, closed-end funds, non-common | ✓ | **partial** — 13F parse already keeps SH/common; index seed gated via OpenFIGI `securityType2 == "Common Stock"` |
| **Event handling** — suspended / M&A / delisting | stale-price / no-recent-trade ⇒ suspended; delisting payoff | ~ | **partial** — suspended proxied by no recent trading; M&A-target removal needs deal data (**✗**), disclosed |
| **Representativeness** — descriptor completeness | ≥N of 10 descriptors present | ✓ | **buildable now** — already the `≥6/10` rule in `regress_factors` |
| **Min cross-section** | ≥30 valid names/date else skip | ✓ | already a gate in `regress_factors` |
| **Stability buffers** | enter/exit percentile hysteresis on size/ADV rank | ✓ | **new mechanic, included** (see below) — the one genuinely new piece |

Two filters can't be done honestly on free data — **free float** and **confirmed-M&A-target
removal** — so they appear as explicit *unavailable* stages with the reason, never silently skipped.
Everything else is PIT and mostly computed from data already in the `tmp/` cache.

**Cost driver:** the population pull. `population(t)` is the PIT S&P 500 (~500/month; ~1,000 distinct
tickers across 2016–2024 once delisted members are included). Most are already in the warm `tmp/`
cache from the build's S&P-500 seed; the slow step is the cold price+fundamentals pull for the
since-delisted members not currently pulled (throttled/cached like every builder pull). Much cheaper
than the earlier ~2–3k pool. Still diagnostic-only — six frames, cube and contract untouched; the
funnel is a separate artifact.

### Architecture (mirrors Phase 1)

1. **`barra_universe_funnel.py`** — builder-side precompute (imports `barra_build_frames` plumbing,
   reuses the warm `tmp/` cache). Assemble `population(t)` = the PIT S&P 500 as-of each month from the
   hanshof change log (Phase 1's parser); for each month and each member compute the PIT metrics
   (mcap, history length, ADV, trading frequency, security type, suspended-proxy,
   descriptor-completeness count); apply the filter stack in a fixed order; record the **first**
   criterion that drops each name (so every name has a verdict and a reason). Writes
   `data/universe_funnel.parquet` — name-level: `(month, position, ticker, issuer, mcap, hist_days,
   adv, trade_freq, sec_type, suspended, n_descriptors, held, stage_dropped, survived)`. The fixed
   stage order: **listing/sec-type → size → history → trading frequency → liquidity/ADV → completeness
   → stability buffer** (free float and M&A-target stages present but inert/unavailable).
2. **`GET /funnel?date=`** — reads only the parquet. Returns the per-month funnel counts (input →
   surviving, with drop counts per stage), the survivor count + how many survivors are held, and the
   drop list for the selected month (name + the criterion that killed it + the failing metric value).
   No cube, no network.
3. **UI — extend the "🌐 Estimation universe" panel** with a funnel view: a horizontal
   stage-by-stage waterfall (PIT S&P 500 → −listing → −size → −history → −freq → −liquidity →
   −completeness → survivors) for the selected month, a small-multiple of the survivor count over
   time, and a drop-list table (`issuer, ticker, stage_dropped, failing value`). Mark held names. A
   visible caption states the expected near-flat funnel (S&P 500 is pre-curated) so a small drop count
   reads as "DQ confirms clean input," not "filters not working."
4. **Thresholds** live in a small documented config (a `limits.json`-style file) so they're explicit
   and tunable: `min_mcap`, `min_hist_days`, `min_adv`, `min_trade_freq`, `min_descriptors`, the
   allowed security types, and the buffer bands (`enter_pctile`, `exit_pctile`). Every threshold —
   and every *unavailable* stage (free float, M&A) — is shown in the panel: "document all filtration
   criteria" is literal.
5. **Tests — `test_funnel.py`** — pure: each filter predicate at its boundary, the "first failing
   stage wins" verdict logic, stability-buffer hysteresis (a name in the 52nd pctile stays out if it
   was out and the enter bar is 55), funnel counts reconcile (population = survivors + Σ drops), and
   that unavailable stages never drop anyone. Integ: `/funnel` shape + counts reconcile.

### Stability buffers (the one genuinely new mechanic)

Membership is re-evaluated monthly with **different enter/exit thresholds** so names hovering at one
cut-off don't flip in and out: a name enters the universe only above the `enter_pctile` (e.g. 55th of
size/ADV) and leaves only below `exit_pctile` (e.g. 50th). This is stateful across months — the spec
carries last month's membership forward and applies hysteresis. Purpose, per Chris: stop threshold
churn injecting noise into factor estimates for no informational reason.

### Decisions

- **Pre-filter population: LOCKED to PIT S&P 500** (`population(t)` = hanshof change-log snapshot
  as-of *t*). Index component = S&P 500; survivorship-free; held book is coverage-only, not in the
  estimation population.
- **Stability buffers: confirmed in** — built with cross-month hysteresis.

Still to confirm before build:

1. **Filter thresholds** — the numeric bars (`min_mcap`, `min_hist_days`, `min_adv`,
   `min_trade_freq`, `min_descriptors`). Suggest starting from the values already implicit in the
   builder and tightening once the funnel shows the distributions.
2. **Unavailable stages** — confirm free float and confirmed-M&A-target removal stay as disclosed
   *unavailable* stages (vs dropping them from the spec entirely). Recommend keeping them visible so
   the funnel documents what a production build would add.

### Effort / risk

~3–5 days, dominated by the data pull for the broader input universe (warm-cache reruns are cheap;
the cold pull of S&P-1500\built names is the one slow step, and it's throttled/cached like every other
builder pull). The filter logic is small and mostly already exists; the risk is data-quality on the
newly-pulled small-cap tail — which is exactly what the funnel is built to expose.

## Phase 3 — the span / high-confidence check (after Phase 2)

Chris's VALUE/SIZE picture, generalized: does each holding sit inside the factor-space spanned by the
estimation universe? Two reads on the same panel — (a) 2D factor-pair scatter (estimation cloud vs
book names, pick the pair), the literal version of his illustration; (b) a numeric per-holding
in-span flag via bounding-box and/or Mahalanobis distance against the estimation cross-section, so
"high confidence vs extrapolation" is a sortable column, not just a chart. Method choice open.

---

## Sequencing & why this order

Phase 1 is self-contained, needs no deferred decisions, and produces the number that drives the
SP1500-vs-R3000 call — so it's the unblocker for everything in `estimation-coverage-design.md`.
Phases 2–3 reuse the same panel + artifact and inherit the input-universe/filter/span-method
decisions once Phase 1's data tells us how broad we actually need to go.

## Effort / risk

Phase 1: ~2–3 days. Risk is concentrated in the membership data — Track A parsing is
straightforward; Track B is just current ETF holdings. No cube/frame/contract change by construction,
so the blast radius is one new script + one endpoint + one panel + one test file. The survivorship
and ticker-drift caveats are real but disclosed, not silent.

## Open questions for Chris (Phase 1 surfaces, doesn't need)

- Is a current-membership proxy for S&P 1500 / Russell acceptable for the "outside" sizing, or does
  he want true PIT there (which on free data we can't cleanly do)?
- Ticker-drift on the historical match — tolerable for a POC read, or worth a CUSIP-based
  reconciliation?
