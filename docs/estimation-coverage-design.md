# Design note — estimation vs. coverage universe

Status: proposal, for review (incl. Chris @ Soros risk) before any builder change.
Scope: `python_src/barra_build_frames.py` (v2). The cube and the six-frame contract are unchanged.

## The point Chris raised

> Estimation universe needs high-quality data, used for the estimation of the model. Coverage
> universe needs to contain all names on which you have (or may want) positions. The universes
> change monthly. Choosing the universe has an impact on the results. Cap the loadings in the
> estimation universe, don't cap the coverage universe — a small name measured against an SPX
> estimation universe *should* read a very negative size loading, and that is correct.

## What the prototype does today

There is **one** universe. `build_frames` builds a single `sec` list = the 13F-held names ∪ an
index seed (`SEED_INDEX = "sp500"`), capped at `UNIVERSE_CAP`. That same set is used for **both**:

1. **Estimation** — the daily cross-sectional WLS that produces factor returns (`regress_factors`).
2. **Carrying positions / loadings** — every name in it gets z-scored loadings (`build_exposures`).

And `_winsor_z` **caps every loading at ±3σ** for every name, estimation or not. So today we do the
opposite of Chris's last point: we cap the coverage names too. A genuinely tiny name's size loading
is clipped to −3 instead of reading its true, larger-magnitude value.

Already satisfied (worth stating to Chris): **all data is point-in-time** — fundamentals are as-of
joined on the SEC `filed` date, 13F weights as-of joined by filing date — and the universe is
**monthly** (the calendar is month-ends). The 0-fill of missing loadings he mentions is also already
how the regression handles thin names (≥6/10 loadings kept, rest zero-filled).

## Proposed split

Two named universes, both rebuilt monthly:

| Universe | Membership | Role | Loadings capped? |
|---|---|---|---|
| **Estimation** | high-quality liquid names — the index seed (SPX), with a data-quality gate (price history, valid mcap, ≥N of the descriptors) | regress factor returns here only | **Yes** — winsorise, so one bad name can't blow up the regression |
| **Coverage** | estimation ∪ every 13F-held name (and any candidate watchlist) | assign loadings to all of these | **No** — z-score against the estimation cross-section's median/MAD, but **do not clip** |

The estimation universe is the clean cross-section the model is fit on. The coverage universe is
"every name we could have a position on" — it must always contain the book, and its loadings are
*assigned* by standardising each name against the **estimation** universe's stats, uncapped.

### Why uncapped coverage matters (Chris's example)

If estimation = SPX and a held name is far smaller than anything in SPX, standardising its raw Size
against the SPX median/MAD gives a large-negative z (say −6). That is the **correct** statement: it
is six estimation-universe MADs below the median. Clipping it to −3 understates a real exposure and
mis-states the book's true size tilt. Capping belongs only in estimation, to protect the regression
from a corrupt or extreme descriptor contaminating every factor return.

## Concrete builder changes (`barra_build_frames.py`)

1. **Build two name lists.** `estimation_sec` = the index seed passed through a data-quality gate;
   `coverage_sec` = `estimation_sec ∪ held names`. (Today's `sec` ≈ the union; the new work is
   *labelling* which names are estimation-grade.)
2. **`build_exposures` takes the estimation set as the standardisation reference.** Compute each
   month's `median`/`MAD` per descriptor over **estimation names only**, then apply
   `(x − med)/MAD` to **all coverage names**. Winsorise (±3) only the estimation rows used by the
   regression; emit coverage loadings **uncapped**. (`_winsor_z` splits into `_winsor_z_estimation`
   (clip) and `_z_coverage` (no clip, same med/MAD).)
3. **`regress_factors` runs on estimation names only.** Factor returns come from the clean set; the
   held-but-not-estimation names never enter the regression (they get loadings + specific risk via
   the assigned loadings and the residual machinery, but don't influence the factor returns).
4. **Specific risk** for coverage-only names: still the EWMA of their own daily residuals against the
   estimation-fitted factor returns. No change to the diagonal-block assumption here (linked
   securities are a separate note).

Nothing downstream changes: the six frames keep their schema, so the cube, the API, and every
measure are untouched. This is purely a change to *how the loadings and factor returns are produced*.

## What this unlocks

- **Correct loadings for off-index holdings** (the uncapped-coverage point).
- **A cleaner factor-return estimate** (regression no longer pulled by illiquid/odd names).
- **A foundation for active risk** — once coverage is explicit, a benchmark (e.g. SPX weights) is
  just another coverage portfolio, and active = book − benchmark exposures. (Separate build.)

## Open questions for Chris

1. **Estimation universe definition / breadth.** Proposal: estimate on the **S&P 1500** (broader,
   more stable daily cross-section, still clean data) rather than the current S&P 500 seed — and let
   **coverage** carry Russell-3000-level breadth as the book needs. Not raw R3000 for *estimation*:
   free-source data degrades on micro-caps and that noise pulls every factor return (Chris's
   "high-quality data" point). Two caveats: index membership should be point-in-time (we currently
   apply today's list across history — a survivorship bias, worse the broader we go), and the per-name
   pull scales ~3× (SP1500) / ~6× (R3000). Settle by an empirical sweep (factor-return stability /
   condition number, names-per-day & `<30` skip rate, Kupiec/Basel backtest). Open for Chris: SP1500
   vs a screened R3000? required liquidity/market-cap screen? PIT membership source? Minimum
   data-quality bar (how many of the 10 descriptors must be present)?
2. **Coverage breadth.** Held names only, or held ∪ a candidate watchlist we maintain?
3. **Capping policy.** Hard ±3σ on estimation, fully uncapped coverage — or a looser coverage cap
   (e.g. ±10) as a corrupt-data backstop while still letting genuine large tilts through?
4. **Monthly membership drift.** Re-pick both universes each month-end (matches "universes change
   monthly"); any stability/turnover constraint wanted on the estimation set?

## Effort / risk

~1 week. Contained to the builder; the cube/API/tests downstream are unaffected by construction. The
main validation is a before/after on factor returns and on the book's headline risk numbers, plus a
spot-check that off-index holdings now read sensible uncapped loadings.
