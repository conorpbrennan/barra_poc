# It's Just Beta — offline index

Snapshot of https://www.itsjustbeta.com taken 2026-07-02. One file per page, each with a
grep-friendly summary, a key-terms line, and every link on the page. Search this directory
instead of re-fetching the site.

**What the site is:** an equity factor-model primer built around `r = Xf + ε` — how factor
models are constructed (universes, cross-sectional WLS, risk assembly) and used (risk
attribution, performance attribution, hedging, alpha research). Written by **Chris**
(ex-Deutsche Bank, BlueMountain, Citadel; studied under Gene Fama) — see `about.md`.
Contact renders obfuscated on-site; recorded as chris@itsjustbeta.com. A self-contained
`mini_example.py` (chapter 18) reproduces every number in the primer.

**Unpublished as of 2026-07-02:** chapters 11, 12, 13, 14, 15 are "Coming soon"
placeholders — re-fetch those when they ship. All other pages have full content.

**Analyses in this directory:** [pnl-attribution-comparison.md](pnl-attribution-comparison.md)
(chapter 10 vs our Step-15 implementation + the Vite UI alignment) ·
[additional-views-plan.md](additional-views-plan.md) (full-primer gap analysis → ranked list of
views to build; all ten built) · [risk-manager-read.md](risk-manager-read.md) (chapter-grounded
desk read of the ten views on live numbers + the keep/remove scope proposal).

## Pages

| File | Chapter | One-line gist |
|---|---|---|
| [home.md](home.md) | Homepage | The `r = Xf + ε` premise + full table of contents. |
| [about.md](about.md) | About | Author background, site aim (practical guidance for hedge funds / family offices). |
| [01-introduction.md](01-introduction.md) | 01 Why Factor Models Exist | Covariance dimensionality problem; CAPM→APT→Fama-French→Rosenberg history; three model families; five applications. |
| [02-the-factor-model-equation.md](02-the-factor-model-equation.md) | 02 The Factor Model Equation | `r = Xf + ε`, assumptions A1–A3, `Σ = XFX' + Δ`, portfolio risk split, five-stock worked example. |
| [03-factors-and-exposures.md](03-factors-and-exposures.md) | 03 Factors and Exposures | Descriptor→z-score pipeline (cap-weighted mean, equal-weighted SD), winsorization, industry dummies, look-ahead bias, factor admission criteria. |
| [04-model-types.md](04-model-types.md) | 04 Types of Factor Model | Time-series/macro vs cross-sectional/fundamental (Barra/Axioma) vs statistical PCA vs hybrid. |
| [05-universes.md](05-universes.md) | 05 Estimation & Coverage Universe | Fit on a clean PIT sqrt-cap-weighted core, extend exposures/specific risk to coverage; span/extrapolation flags; linked-asset blocks. |
| [06-estimating-factor-returns.md](06-estimating-factor-returns.md) | 06 Estimating Factor Returns | Per-period cross-sectional WLS `f̂ = (X'WX)⁻¹X'Wr`; cap-weighted industry constraint; winsorization/Huber; thin-factor shrinkage; R²/t-stats. |
| [07-factor-portfolios.md](07-factor-portfolios.md) | 07 Factor Portfolios | Factor returns as pure long-short portfolio returns (`f̂ = Pr`, `PX = I`); purity-vs-leverage tradeoff; Fama-French sorts; Fama-MacBeth. |
| [08-risk-model-assembly.md](08-risk-model-assembly.md) | 08 Risk Model Assembly | EWMA factor covariance (split vol/corr half-lives, Newey-West, shrinkage); specific-risk blend by credibility γ; bias statistic `b ≈ 1 ± √(2/T)`. |
| [09-risk-attribution.md](09-risk-attribution.md) | 09 Risk Attribution | `σ² = x⊤Fx + w⊤Δw`; contribution-to-variance / MCR / CTR; active/TE view; correlated-shock stress. |
| [10-performance-attribution.md](10-performance-attribution.md) | 10 Performance Attribution | Exact single-period `r_a = x_a⊤f + w_a⊤ε`; Cariño/Menchero/Frongello linking; skill-vs-tilt (`t = IR√T`); Brinson comparison; four pitfalls. **Compared against our Step-15 `/pnl_attribution` — see pnl-attribution-comparison.md.** |
| [11-portfolio-construction.md](11-portfolio-construction.md) | 11 Portfolio Construction | **Placeholder — coming soon.** |
| [12-hedging.md](12-hedging.md) | 12 Hedging | **Placeholder — coming soon.** |
| [13-alpha-research.md](13-alpha-research.md) | 13 Alpha Research | **Placeholder — coming soon.** |
| [14-model-evaluation.md](14-model-evaluation.md) | 14 Evaluating a Factor Model | **Placeholder — coming soon.** |
| [15-modifying-the-model.md](15-modifying-the-model.md) | 15 Modifying a Factor Model | **Placeholder — coming soon.** |
| [16-practical-considerations.md](16-practical-considerations.md) | 16 Practical Considerations | PIT databases, delisting returns, six-gate production pipeline, numerics (QR/SVD, Woodbury, never form Σ), eight-pitfall checklist, factor zoo, build-vs-buy. |
| [17-appendix.md](17-appendix.md) | 17 Appendix | Notation, OLS/WLS/GLS/EWMA/Marchenko–Pastur reference, six derivations, glossary, the 10-stock mini-example dataset, bibliography. |
| [18-mini-example-source-code.md](18-mini-example-source-code.md) | 18 Mini Example Source Code | Self-contained `mini_example.py`: universe → exposures → constrained WLS → risk assembly → stress → Cariño attribution → hedging → optimization. |

## Site link map (canonical URLs)

- https://www.itsjustbeta.com/ — home
- https://www.itsjustbeta.com/about/ — about
- https://www.itsjustbeta.com/chapters/01-introduction/
- https://www.itsjustbeta.com/chapters/02-the-factor-model-equation/
- https://www.itsjustbeta.com/chapters/03-factors-and-exposures/
- https://www.itsjustbeta.com/chapters/04-model-types/
- https://www.itsjustbeta.com/chapters/05-universes/
- https://www.itsjustbeta.com/chapters/06-estimating-factor-returns/
- https://www.itsjustbeta.com/chapters/07-factor-portfolios/
- https://www.itsjustbeta.com/chapters/08-risk-model-assembly/
- https://www.itsjustbeta.com/chapters/09-risk-attribution/
- https://www.itsjustbeta.com/chapters/10-performance-attribution/
- https://www.itsjustbeta.com/chapters/11-portfolio-construction/ (placeholder)
- https://www.itsjustbeta.com/chapters/12-hedging/ (placeholder)
- https://www.itsjustbeta.com/chapters/13-alpha-research/ (placeholder)
- https://www.itsjustbeta.com/chapters/14-model-evaluation/ (placeholder)
- https://www.itsjustbeta.com/chapters/15-modifying-the-model/ (placeholder)
- https://www.itsjustbeta.com/chapters/16-practical-considerations/ (placeholder)
- https://www.itsjustbeta.com/chapters/17-appendix/
- https://www.itsjustbeta.com/chapters/18-mini-example-source-code/

(Per-page link lists, including prev/next navigation and any external references, are in each
page file's `## Links` section.)

## Searching

`grep -ri <term> itsjustbeta/` — summaries keep the site's verbatim terminology
(e.g. "Cariño", "bias statistic", "estimation universe", "pure factor portfolio",
"Marchenko-Pastur", "trading residual") so keyword search lands on the right chapter.
