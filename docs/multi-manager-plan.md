# Design note — multi-manager 13F integration

Status: **BUILT** 2026-07-30, across five phases (0 recon, 1 builder, 2 cube, 3 API, 4 frontend), all
uncommitted on `docs/vite-ui-plan` as of this note. Scope: `python_src/barra_build_frames.py`,
`python_src/barra_factor_risk_cube.py`, `python_src/risk_api.py`, `python_src/barra_universe_funnel.py`,
`limits.json`, `frontend/`. The six/seven/eight-frame contract and the cube's scenario engine are
otherwise unchanged.

> **What was built:** the book widens from Soros-only to **11 managers' 13F books** (`MANAGERS` in
> `barra_build_frames.py`), each its own `Book` in the existing `positions` frame, sharing the same
> exposures/factor-return/specific-risk machinery. An optional 8th frame (`managers`) carries per-book
> entity metadata and disclosure stats. The cube gained a separate `Manager` entity hierarchy. The API
> gained per-book guards on every artifact that was built assuming a single book, plus a hard block on
> the three attribution measures that genuinely cannot be made per-book on this atoti version. The
> frontend gained a real book/entity selector, degrading to plain text when only one book is loaded
> (today's actual state — nothing about the running service's behaviour has changed yet, since a
> multi-manager `build_frames()` has not been run against production `data/`).

## Why

The model was proven against one book. The next question — the whole reason to have a multi-manager
model — is whether the same factor engine reads sensibly across managers with very different mandates
(macro, multi-strat, quant, activist), and whether the desk's tooling (limits, drift, attribution,
universe diagnostics) generalizes or needs to be book-aware. This phase answers both: it wires up the
data plumbing for real, and it finds (and discloses, rather than papers over) exactly where the existing
single-book assumptions break.

## The eleven books

CIKs verified against EDGAR (exact-name `browse-edgar` query + a broader root-name query to surface
false positives, then cross-checked against `submissions/CIK##########.json` for entity name, filing
count, and date range — `scratchpad/phase0-recon.md`):

| Book | CIK | Style | Latest-filing scale (recent-only, pre-pagination, phase0b) |
|---|---|---|---|
| Soros | 1029160 | Long/short equity | $5.53bn, 229 CUSIPs |
| Bridgewater | 1350694 | Global macro | $22.40bn, 993 CUSIPs |
| Citadel | 1423053 | Multi-strategy | $138.7bn, 5,960 CUSIPs |
| Millennium | 1273087 | Multi-strategy | $127.7bn, 3,735 CUSIPs |
| Renaissance | 1037389 | Systematic quant | $63.93bn, 3,213 CUSIPs |
| AQR | 1167557 | Systematic quant / factor | $218.4bn, 3,739 CUSIPs |
| Two Sigma | 1179392 | Systematic quant | $117.1bn, 3,593 CUSIPs |
| D. E. Shaw | 1009207 | Multi-strategy quant | $119.0bn, 3,102 CUSIPs |
| Point72 | 1603466 | Multi-strategy | $54.89bn, 1,904 CUSIPs |
| Tiger Global | 1167483 | Long/short equity — growth | $22.85bn, 54 CUSIPs |
| Elliott | 1791786 + 1048445 | Event-driven / activist | $15.90bn, 17 CUSIPs (current entity only) |

Rejected alternates for every firm are logged in `scratchpad/phase0-recon.md` — mostly unrelated RIAs
that share a root name (e.g. RenaissanceRe Holdings, a Bermuda reinsurer, is not Jim Simons's fund) and
regional/offshore affiliate filers (Point72's DIFC/Hong Kong/Singapore/London/Middle East entities each
file their own 13F; only the US flagship L.P. is modelled, consistent with "one filer = one book").

### Two Sigma Advisers (1478735) — deliberately excluded

Two entities file substantial, ongoing 13F-HRs under the "Two Sigma" name: Two Sigma Investments, LP
(1179392, 94 filings back to 2002, chosen) and Two Sigma Advisers, LP (1478735, 62 filings back to
2010). Recon flagged this as a genuine scope question, not an identity problem — they're reported to
run different books, not duplicates. Measured before deciding: Two Sigma Advisers' **latest 13F table
is one name at $0** (dormant — the same shape as Elliott's superseded predecessor CIK once the
successor takes over) and its full-history CUSIP set contributes only **7 incremental CUSIPs** beyond
what Two Sigma Investments already covers. Excluded on the numbers, not a coin flip: it isn't a second
book worth carrying, it's a filer that stopped mattering. Documented in the `MANAGERS` comment block so
a future reader doesn't "fix" the omission without re-measuring.

### Elliott — two CIKs, one book

Elliott renamed/re-filed under a new legal entity in 2020: Elliott Management Corp (1048445) filed
1999-09-30 through 2020-09-30; Elliott Investment Management L.P. (1791786) filed 2020-03-31 onward.
The two overlap on exactly three report dates (2020-03-31, 2020-06-30, 2020-09-30) — both CIKs filed a
13F-HR for each. `MANAGERS`'s `cik` field takes a tuple for this case, current entity first;
`stitch_multi_cik` prefers the first (current) CIK on any report_date both cover, keeps every
non-overlapping date from both. **Not verified**: whether the two entities' filings for the three
overlap quarters are identical or materially different holdings — that would need pulling and diffing
both info tables, judged out of scope for wiring up the stitching policy itself. Flagged as a follow-up.

## Getting to real history: the `filings.files` pagination fix

`positions_from_13f` originally read only `filings.recent` — SEC's index of an entity's most recent
filings **of any form type**, capped at roughly 1,000 entries. A CIK with heavy non-13F filing volume
(Citadel, Renaissance — both file constantly) pushes its older 13F-HRs out of `recent` even though the
entity filed 13F-HR continuously for decades. Measured: Renaissance's `recent` window started at
**2019-12-31**, four years short of the model's 2016-01-01 start; Citadel's started at **2016-03-31**,
missing about six weeks right at the start.

Fix: `_13FHR_filings_all(cik)` fetches `filings.recent` **plus every `filings.files` pagination
block** (the same cached `_get_json`), filters to `form == "13F-HR"`, dedupes on `accessionNumber`.
Verified live, not just unit-tested: Renaissance and Citadel (and, as a side effect, Soros) now all
parse from **2013-06-30**. That date is a hard floor for every manager, pagination or not — inspecting
a pre-2013-06-30 Soros filing directly shows it's a plain `.txt` document with no XML info-table; SEC's
machine-readable 13F info-table format is mandatory only from mid-2013 onward, and the existing parser
already (correctly) skips filings it can't find an XML table in. So the pagination fix's actual, useful
effect is recovering 2013-06-30 through each manager's previous `recent` floor — three extra years of
margin inside the model's 2016-2026 window, not closing a gap inside it.

## The ETP filter

13F info tables include ETFs, index funds, and (since 2024) spot commodity/crypto trusts — vehicles
with no XBRL fundamentals and no sector, which a cross-sectional Barra-style regression cannot
meaningfully assign a style/industry loading to (an ETF is a diversified basket, not a single-name
factor bet). `DROP_ETPS` (default on) removes them via a word-boundary regex over a curated brand-name
token list (`ETF, SPDR, ISHARES, VANGUARD, VANECK, PROSHARES, KRANESHARES, DIREXION, GRAYSCALE,
BITWISE, "SELECT SECTOR", "INVESCO QQQ", "INDEX FDS?", "TIDAL TRUST"`), never a bare-substring match —
confirmed against the recon's matched-issuer sample that a naive substring check both false-positives
on real operating companies whose legal name contains a common word (MEDICAL PROPERTIES TRUST, KITE
REALTY GROUP TRUST, AMERICOLD REALTY TRUST, REDWOOD TRUST, CONSUMER PORTFOLIO SVCS, ALTISOURCE
PORTFOLIO SOLUTIONS all correctly survive) and spuriously matches ordinary words containing a token
substring ("NETFLIX" contains "ETF" — word-boundary matching rejects this). `INVESCO` is deliberately
scoped to `INVESCO QQQ` only, since Invesco Ltd (ticker IVZ) is a real, holdable operating company.

Measured value share dropped from each manager's latest filing (full parsed history, post-pagination
fix): **Bridgewater 24.47%, Millennium 11.64%, Citadel 6.76%, Soros 1.94%, Tiger Global 0.00%.**

**Stated plainly, not softened**: Bridgewater loses roughly a quarter of its 13F by value to this
filter. Bridgewater's public 13F is heavily commodity/index-ETF and, since 2024, spot Bitcoin/Ethereum/
Gold/Silver trust exposure — none of it equity, none of it has a sector or fundamentals, none of it is
representable by this model's descriptors. What's left after the filter is a small, equity-only slice
of Bridgewater's real macro book, not a fair read of the fund. This matters for interpreting any
Bridgewater risk number this model produces — it should not be read as "Bridgewater's risk," only as
"the equity residue of Bridgewater's 13F, post-ETP-filter." The disclosure (dropped rows/value/CUSIPs,
both latest-filing and full-history) is carried per-book on the `managers` frame precisely so this
caveat travels with the number, not just in this doc.

## Universe scale and `UNIVERSE_CAP`

The measured union of held CUSIPs across all 11 books is **22,113 distinct CUSIPs, 21,047 of them new**
against the previous single-book (Soros-only) build's 1,199 securities. `UNIVERSE_CAP` — which this
repo's own CLAUDE.md still described as `250` going into this phase (already stale; the real
pre-integration value was 3500) — is raised to **35000**. This isn't a round-up for headroom's sake:
`build_frames()`'s `sec.head(UNIVERSE_CAP)` truncates the universe **order-dependently and silently** —
whatever falls past the cap in whatever order `sec` happens to be sorted in just disappears, no
warning, no error. At 3500 the cap would have been exceeded several times over by Renaissance's history
alone (7,603 new CUSIPs measured from its `recent`-only, still-truncated pre-pagination-fix window) —
it would have silently kept an arbitrary ~16% of the union and dropped the rest. 35000 leaves real
headroom above the measured 22,113, not just above what today's scoped test builds have needed.

## The cube: an entity dimension, and the landmine that turned out not to bite

Phase 2 added the optional `managers` frame as a **partial join on `Book`** off the existing `Positions`
table, backing a new `Manager` hierarchy (`FirmType`/`EntityName`/`CIK`) and three disclosure measures
(`Manager ETP dropped value share`, `Manager n filings`, `Manager n positions`). Deliberately kept as a
**separate** hierarchy rather than extra levels on the pre-existing, auto-created, single-level `Book`
hierarchy — every `l["Book"]` lookup in the rest of the cube and in `risk_api.py` stays untouched.

Two things that looked like they might be landmines were investigated and found to be correct as-is,
with no code change needed:

- **`Top-5 risk share` / the `PositionRank` hierarchy.** `tt.rank` evaluates each candidate's measure
  value at the *current query context*, which already includes whichever `Book` is sliced (Book was
  never one of the hierarchies passed to the rank call, so it just stays pinned). Verified on real
  2-book test data against an independent numpy computation: diffs at float-noise level (~1e-17) for
  both books, and the two books' rankings provably differ (not a coincidence of both reading the same
  wrong number).
- **`tt.total` lifting.** All 15 `tt.total(...)` call sites in the cube already lift exactly
  `{Security, FactorDim, PositionRank}` and never `Book`/`Date`/`ScenarioSet` — "book total" has always
  meant "total for whichever book/date/scenario-set is currently sliced," not "total across every
  book." Adding `Manager` doesn't change this since it's never passed to a `tt.total`/`OriginScope`
  call either. Verified via the Euler identity (`Σ Marginal Model vol == Model vol`) within a
  single-book slice on real 2-book data: residuals ~1e-16, both books.

A third issue, **not one of the two named landmines**, was found and is the single most consequential
limitation of this integration:

### The book-independent attribution measures

`Factor contribution`, `Specific PnL`, and `Realized PnL` are additive cube measures built on physical
columns baked at cube-build time from a pandas merge keyed on `(Date, Position)` — deliberately with no
`Book` key, so these measures stay immune to the what-if hypothetical-trades branch (which lives on
`Positions`' live, branchable weight; a static baked column can't read a transient branch). With the
original single-book data this was harmless — there was only one book's weight to bake in. With more
than one book, the merge silently produces duplicate-keyed rows for any name held by more than one
manager on the same date (confirmed: the Exposures leaf-row count roughly doubled on a 2-book test
build), and `Factor contribution` read an *identical* number for both books on a date where they held
materially different weights of the same name.

The natural fix — reuse Book as a second hierarchy dual on the attribution side table, alongside the
existing Factor dual from Exposures — was attempted and **empirically blocked by atoti 0.9.15**: every
join topology tried (a single edge from either parent table, and a two-edge "diamond" mapping Book via
Positions and Factor via Exposures simultaneously, tried in both edge orders, including explicit
post-hoc hierarchy redefinition) left one of the two axes as an unresolvable ambiguous `Book` hierarchy
(`ValueError: Disambiguate 'Book' to narrow it down to one of [('Positions','Book','Book'),
('FactorPnL','Book','Book')]`) — one repro even built successfully and only failed at query time. This
is a concrete Atoti API/architecture limit on this version, established by direct experiment, not
inferred from documentation.

**What shipped instead**: the merge was hardened (deduped to one deterministic row per `(Date,
Position)`, first book alphabetically) so it can no longer silently corrupt the leaf tables with
duplicate keys — but the three measures are **not** made book-aware by this. For a name held by more
than one manager, they read that one arbitrary book's weight under every book's label, regardless of
which book the query is actually sliced to. Since Phase 3, `_validate_pivot` — the one function
`/pivot`, `/analysis`, and `/ask`'s `query_cube` tool all call before touching the cube — **rejects
these three measures outright once more than one book is loaded**, with a message that names the
mechanism and points at the correct alternative (`barra_pnl_attribution.py`'s `book=` parameter, which
computes attribution live from the raw frames, correctly scoped to one book, at the cost of not being a
live cube drill). Single-book behaviour — today's actual production state — is untouched: the guard
only fires once `positions["Book"].nunique() > 1`.

**Follow-up, not attempted here**: either confirm a supported way to alias one table's column onto an
already-established hierarchy from a second table in this atoti version (possibly fixed in a later
release, or achievable through an API surface not tried), or build N book-keyed physical tables in a
loop over the active books so each book gets its own attribution column. Either would let the guard be
relaxed to genuine per-book correctness instead of an outright block.

## The API: per-book guards, not per-book correctness

Several precomputed artifacts and live-frame reads were written assuming exactly one book, with no
per-book scoping mechanism of their own:

- `barra_universe_membership.py` hardcodes `SOROS_CIK` and never reads `positions.parquet` — its
  coverage is fixed at build time, independent of whatever books the live frames hold.
- `barra_universe_funnel.py`, `barra_universe_span.py`, `barra_universe_drift.py`, and
  `barra_pnl_attribution.py`'s `run()` all read (or were called against) whatever book(s) happened to be
  in `positions.parquet` at build time, with no Book filter baked into the artifact's own schema.

Serving any of these under a different book's label would be silently wrong, not merely stale — worse
than an error. `_artifact_book(kind)` resolves, as best it can, which book a given artifact actually
covers: for `membership` it's deterministic (`SOROS_CIK` resolved through `barra_build_frames.MANAGERS`
rather than a hardcoded string, so a future rename of that book's label can't silently desync); for the
other four kinds, it infers from the **live** `positions` frame — exactly one distinct `Book` means
that's (almost certainly) what the artifact covers, more than one means "can't verify."
`_book_guard(kind, requested_book)` wraps this into a clean HTTP 200 with
`{"status": "book_mismatch", "kind", "requested_book", "artifact_book", "basis", "reason"}` on a
mismatch — mirroring `/drawdown`'s existing `status: "insufficient"` idiom rather than inventing a new
shape family. Wired into all eight affected endpoints (`/universe`, `/funnel`, `/span`, `/drift`, the
four `/pnl_attribution*` routes), each of which gained a `book` query parameter defaulting to `"Soros"`
— today's data is genuinely single-book, so every existing caller that never sends `book` sees zero
behaviour change.

**Disclosed weakness, not fixed**: `_artifact_book`'s inference reads today's *live* positions frame,
not a stamp recorded at the artifact's own build time. If an artifact goes stale relative to the
currently-loaded frames — built while Soros was the sole book, then the frames later swapped to a
different single-book set without rerunning the precompute — the helper would report the new book as a
match and wave a genuine mismatch straight through. There's no version linkage anywhere in the
frame/artifact contract that could catch this; fixing it would mean the precomputes each writing their
own covered-book stamp and the guard checking it instead of inferring, which wasn't done here.

`barra_universe_funnel.py` additionally had a narrower, real bug latent inside the single-book
assumption: its `held`/`held_survivors` flag used to mean "held by the union of all rows in
`positions`," which is silently "held by ANY manager" the moment a second book exists — not a guard
question, an actual wrong-answer risk. Fixed with `_held_positions(pos, book)`; `run()`'s new
`book: str | None = "Soros"` parameter defaults to reproducing today's exact behaviour (`book=None` is
kept as an explicit escape hatch to the old any-book-union reading, unused by any caller in the repo).

### `/limits` — additive disclosure, not per-book thresholds

`limits.json` gained one additive field, `calibrated_for` (default `"Soros"` when absent, matching what
was always implicitly true). `/limits`'s response gained `calibrated_for`, `cross_book_thresholds`
(bool), and `calibration_note` (a plain sentence, present only when the flag is true) — all additive;
no existing consumer's keys changed. This surfaces the caveat rather than hiding it: **the desk limit
thresholds remain a single flat set, tuned against Soros's scale and strategy, and have not been
recalibrated per book.** Reading Bridgewater's or Citadel's Scenario VaR against Soros's warn/limit
bands is directionally informative but not a real per-manager risk budget.

## The frontend: an entity selector that earns its ink

`/meta.managers` is the UI's one source for the entity list (`_managers_meta()`, mirroring the existing
`hypo_shocks` precedent: server-sourced, never hardcoded, so the UI can never drift from whatever
`ACTIVE_MANAGERS` scope the running build actually used). The context bar's book field renders **plain
text when only one manager is loaded** and a real `<select>` once two or more exist — a `<select>` with
one immutable option is chartjunk (ink spent on a control that cannot do anything); the interactivity
itself is what's hidden until there's a genuine choice, the same principle already applied elsewhere in
the app (`HowToRead`'s collapsed `<details>`). Every guarded lens (Universe, Drift, the Attribution
PnL tab, Overview's reconcile strip) renders the `book_mismatch` payload as a restrained, non-alarming
notice (`BookMismatchNotice` — `muted small`, never the red `.err` class, because this is a disclosed
expected state, not a failure) instead of crashing on an unexpected response shape.

## What was NOT done — deliberately out of scope

- **Per-entity precomputes.** `barra_universe_membership/funnel/span/drift.py` and
  `barra_pnl_attribution.py` were not rewritten to natively carry a `Book` dimension on their own
  artifacts — the Phase 3 guard makes the single-book assumption *safe* (fails closed with a clear
  status), not *correct* for every book. A proper per-book artifact story would give each of the 11
  managers their own universe-membership/funnel/span/drift/attribution read; that's real, scoped
  follow-on work, not a quick patch.
- **Per-book limits.** `/limits`'s thresholds stay one flat Soros-calibrated set; see above.
- **A cross-entity comparison lens.** Nothing in this phase lets a user put Bridgewater's and
  Citadel's risk numbers side by side in one view. Given the caveat below, that comparison would need
  to be built carefully (normalizing for what a 13F even represents for each style of fund), not bolted
  on as a naive multi-select.
- **An 11-manager end-to-end `build_frames()` run.** Every verification in this phase (Phase 1's
  builder plumbing, Phase 2's cube joins, Phase 3's API guards, Phase 4's frontend wiring) was checked
  against 2-book synthetic or scoped test data (`ACTIVE_MANAGERS = ["Soros", "TigerGlobal"]`,
  hand-built 2-book cube fixtures) or read-only `positions_from_13f` calls against individual managers
  — never a real 11-manager, ~35,000-name production build. Performance and correctness at that full
  scale (particularly `tt.rank` over a `PositionRank` hierarchy with tens of thousands of mostly-zero
  members across 11 books) is unverified.

## The honest caveat: a 13F is not a fair cross-manager risk metric

A 13F filing is a **long-only, US-listed-equity, quarterly snapshot filed up to 45 days after quarter
end** — no shorts, no derivatives (options are explicitly filtered out by the existing cash-equity
filter), no non-US-listed holdings, no intra-quarter trading. For a fund whose real book *is*
substantially that — a long-only or long-biased US equity manager like Soros or (its equity residue,
after the ETP filter) Bridgewater — the 13F is a reasonably fair, if stale and incomplete, read. For a
genuinely hedged multi-strategy fund like Citadel or Millennium, whose real risk includes large short
books, derivatives overlays, fixed income, and non-equity strategies entirely invisible to a 13F, the
document is a small and actively misleading slice: a risk number computed only from the long US-equity
book of a multi-strat fund is not "the fund's risk," it can't even reliably be characterized as *net*
long-equity risk, because the offsetting shorts that would net against it are exactly the part the 13F
doesn't show. **Risk numbers from this model should not be compared across those two groups of managers
as if they meant the same thing.** This caveat existed implicitly for the single-book Soros model too;
it becomes load-bearing the moment more than one manager, with different real strategies, is on screen
at once, and belongs in every place this model's multi-manager output gets read by anyone who wasn't in
this build.

## Known open items (see also CLAUDE.md's cross-reference)

1. **Pre-existing, unrelated test failure**: `test_risk_measures.py::t_incremental_total_is_subadditive`
   fails against the live, unmodified cube and unmodified `data/*.parquet`
   (`Σincr=0.04242186115214519 !< book=0.03580909167096196` — a real sub-additivity violation in a risk
   measure). Confirmed present before any of Phases 1–4 touched anything and unaffected by them (neither
   the cube nor the data were touched by the parts of this work that could plausibly move this number).
   Not investigated further here — flagged for whoever owns `barra_factor_risk_cube.py`'s risk measures
   next.
2. **`/dims` reports `Book` members as `['N/A', 'Soros']`** on the live, unrestarted service — an extra
   `"N/A"` member alongside the real book. Cause not identified; plausibly an atoti default member
   surfacing for exposure rows with no matching position. Confirmed not caused by anything in this
   phase (it's pre-existing on the running pre-Phase-3 code) and confirmed the new `_multi_book_cube()`
   helper is unaffected (it reads the `positions` frame directly, which has no such member). The
   frontend filters it out defensively (`m.book !== "N/A"`) wherever a book list is built from `/meta`,
   as a precaution rather than a fix. Worth root-causing before the entity dimension goes live.
3. **OpenFIGI CUSIP-resolution drift makes a rebuild non-reproducible.** Re-running the Soros
   crosswalk today (same code, same CUSIPs) fails to resolve 9 names that resolved when `data/` was
   last built, including Honeywell (CUSIP 438516106 — confirmed live, both through this repo's
   `crosswalk_cusips` and a bare uncached single-CUSIP request bypassing all repo code: it now resolves
   only to non-US listings, GBP/EUR/RUB tickers on a non-US `exchCode`, not the US common stock the old
   FIGI represented). This is an **external OpenFIGI data-availability change**, not a regression in
   this phase's code — no crosswalk logic was touched — but it means **a fresh rebuild will not
   reproduce previously published numbers** for a small, disclosed set of names. Whoever runs the next
   full rebuild should expect this and treat it as a known, external drift, not a new bug.
4. **Per-entity precomputes and per-book limits — not done.** See "What was NOT done" above.
5. **`_artifact_book`'s staleness gap — not fixed.** See the per-book-guard section above; there is no
   artifact-versioning mechanism in the frame contract to catch a stale single-book artifact being
   served against a since-changed set of live frames.

## Effort / risk

Five phases, same day (2026-07-30): recon (read-only, SEC EDGAR queries only), builder (config +
pagination + ETP filter + per-book weight normalization + the 8th frame), cube (one optional frame, one
new hierarchy, three measures, two landmines investigated, one real limitation found and guarded),
API (two guard mechanisms reused across 12 endpoints total, one additive limits disclosure), frontend
(one real selector, guarded rendering on 6 call sites). The main residual risk is exactly what's listed
under "what was NOT done" and "known open items" above — this phase makes the multi-manager plumbing
correct and safe to expose, it does not yet make every existing single-book analytical lens genuinely
correct *for* every one of the 11 books.
