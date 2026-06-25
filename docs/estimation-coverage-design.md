# Design note — estimation vs. coverage universe

Status: **IMPLEMENTED** in `python_src/barra_build_frames.py` (flag `UNCAP_COVERAGE`, default on).
The cube and the six-frame contract are unchanged. Endorsed by Chris (option 1, PIT S&P 500).
Scope: `python_src/barra_build_frames.py` (v2).

> **What was built (2026):**
> - **Estimation universe** = the market-index seed (the S&P 500), flagged `is_estimation` on `sec`.
>   **Coverage** = estimation ∪ every held name; coverage-only = held-but-not-S&P-500.
> - **`_split_z`** standardizes each descriptor against the **estimation** cross-section's median/MAD,
>   **winsorizes estimation rows at ±3**, and leaves **coverage rows uncapped** — with a loose
>   `COVERAGE_CAP = ±10` backstop so a genuine large tilt (a tiny name's Size ≈ −6) survives but a
>   corrupt XBRL value (negative-equity Leverage, etc.) can't produce an `inf`/`−48000` loading.
> - **`regress_factors` fits factor returns on the estimation universe only**; specific residuals are
>   then formed for **every** coverage name against those fitted returns (so a held non-S&P-500 name
>   still gets a specific-risk forecast without polluting the factor returns).
> - Rebuilt: 503 estimation / 693 coverage-only names. **Before → after:** style-loading range
>   −3.1…3.1 → −10.1…10.6; most-negative held Size loading −2.97 → **−6.78** (its true value, was
>   clipped); **style-factor VaR 99 (ex-Market) 0.51% → 1.05%** — the split surfaces book style risk the
>   ±3 clip was hiding; the market-dominated headline Total VaR ≈ 3.6% is unchanged (Market is a
>   structural 1.0 loading); the span check reads ~82% of the book inside on average (latest month
>   70.5%) vs the artificially-high ~90% under the old uniform clip; `factor_returns`/`specific_var`
>   row counts ≈ unchanged; all desk limits green. `UNCAP_COVERAGE=False` reproduces the legacy
>   single-universe behaviour. Full test suite green.

The original proposal and rationale follow.

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

> **Update (2026-06-23): Q1 RESOLVED.** Chris's steer was **option (1), the point-in-time S&P 500** —
> "survivorship bias should be avoided at all costs." That settled it over the S&P 1500 idea below: on
> free data only the S&P 500 has clean PIT membership (the hanshof change log), and the diagnostics
> built since (see `universe-diagnostics-plan.md`) confirm it's well-supported — the funnel over the
> PIT S&P 500 is near-flat (the names are already clean) and the span check shows ~90% of the book
> sits inside the S&P 500's factor space. Broader (S&P 1500 / R3000) would need a paid PIT feed
> (Norgate ~$630/yr or WRDS/Compustat) and is parked unless the book's drift later forces it. The
> remaining estimation-universe membership is defined by **PIT data-quality rules**, not an index
> beyond the S&P 500 seed.

1. ~~**Estimation universe definition / breadth.**~~ RESOLVED (see above). *(Original proposal,
   retained for context:)* estimate on the **S&P 1500** rather than the current S&P 500 seed, letting
   coverage carry R3000-level breadth. Superseded by the PIT-S&P-500 decision.
2. **Coverage breadth.** Held names only, or held ∪ a candidate watchlist we maintain?
3. **Capping policy.** Hard ±3σ on estimation, fully uncapped coverage — or a looser coverage cap
   (e.g. ±10) as a corrupt-data backstop while still letting genuine large tilts through?
4. **Monthly membership drift.** Re-pick both universes each month-end (matches "universes change
   monthly"); any stability/turnover constraint wanted on the estimation set?

## Effort / risk

~1 week. Contained to the builder; the cube/API/tests downstream are unaffected by construction. The
main validation is a before/after on factor returns and on the book's headline risk numbers, plus a
spot-check that off-index holdings now read sensible uncapped loadings.
