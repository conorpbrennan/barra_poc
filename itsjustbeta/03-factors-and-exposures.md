# Factors and Exposures
URL: https://www.itsjustbeta.com/chapters/03-factors-and-exposures/

## Summary
Chapter 3 explains how factor exposures — the building blocks of fundamental equity models — are constructed from raw company data. The exposure matrix X contains three types of columns: market factors (all ones), industry membership dummies (0/1 indicators), and style factors (standardized z-scores).

**Definitions.** A **factor** is a common driver of returns across many stocks. An **exposure** measures a single stock's sensitivity to that factor — an entry in the exposure matrix X.

**Factor taxonomy** (commercial models typically hold 70–90 factors):
- **Market / country factors** — capture broad rallies and sell-offs.
- **Industry / sector factors** — dummy variables under classification schemes like GICS or ICB, spanning 10–60 industries.
- **Style factors** — eight canonical types: value (price ratios), size (market cap), momentum (recent returns), volatility/beta, quality/profitability, growth, leverage, liquidity, dividend yield.
- **Currency factors** — separate equity and currency decisions.
- **Specialty factors** — ESG, crowding, sentiment, machine-learned factors.

**Three-stage pipeline.** Stage 1: raw data → descriptors (single measurable quantities per stock). Stage 2: blend multiple descriptors into a raw factor using standardized inputs, then standardize once more for the final exposure. Stage 3: comparable z-scores.

**Z-score standardization formula:**

    X_ik = (d_ik − μ_k) / σ_k

where X_ik = standardized exposure (stock i, factor k), d_ik = raw descriptor value, μ_k = **cap-weighted mean** of the descriptor over the estimation universe, σ_k = **equal-weighted standard deviation** (population form, dividing by n).

**Blended factor example:**

    RawValue_i = 0.5 z(B/P_i) + 0.3 z(E/P_i) + 0.2 z(CF/P_i)

Descriptors are standardized individually before blending with predetermined weights.

**Standardization operations:**
1. **Winsorize** — clip outliers at ±3 standard deviations from the mean, or MAD-based clipping (more robust: median and median absolute deviation scaled by 1.4826).
2. **Z-score against a reference population** — asymmetric standardization: mean is **cap-weighted** (so the market portfolio has zero style exposure) while the standard deviation is **equal-weighted** (so mega-caps don't dominate the scale).
3. **Handle missing data** — fill from industry/country peers first, then by regression on available descriptors, finally default to zero exposure (the neutral assumption).

**Worked example (MiniModel VALUE factor).** 10-stock universe, market caps totaling ≈ $615 billion. Cap-weighted mean B/P = 0.5049; equal-weighted standard deviation = 0.2890. So:
- AXIOM: X = (0.15 − 0.5049) / 0.2890 = −1.228
- GUARDIAN: X = (1.10 − 0.5049) / 0.2890 = +2.060

**Binary vs continuous exposures.** Market column: all ones. Industry columns: membership dummies (each stock has 1 in exactly one industry, 0 elsewhere). Style columns: continuous z-scores, signed, roughly in [−3, 3], cap-weighted-zero. Industry columns sum to the market column — built-in collinearity, resolved by constraints on factor returns (Chapter 6).

**Exposure dynamics.** The exposure matrix rebuilds at each model date (daily or monthly). Prices move continuously (affecting B/P, size, momentum); financial statements update quarterly; industry membership changes rarely. Momentum turns over rapidly by design; size moves glacially. Crucially, exposures used at date t must derive only from information public before t — filings lagged appropriately, prices through t−1. Historical restatement of fundamentals introduces **look-ahead bias**, "the most expensive class of bug."

**Admission criteria for good factors** (standardization makes any descriptor usable, not necessarily worthwhile):
1. Economic rationale — a defensible story for why the characteristic drives returns.
2. Statistically significant factor returns — meaningful proportion of periods with |t| > 2.
3. Persistence and breadth — works across decades and markets, not one regime.
4. Non-redundancy — low correlation with existing factor exposures and returns.
5. Coverage and data quality — descriptor availability and reliability across the universe.

## Key terms
factor, exposure, exposure matrix X, descriptor, z-score, standardization, X_ik = (d_ik − μ_k) / σ_k, cap-weighted mean, equal-weighted standard deviation, winsorize, MAD, median absolute deviation, 1.4826, missing data, industry dummies, GICS, ICB, market factor, country factor, style factors, value, B/P, E/P, CF/P, book-to-price, earnings yield, size, market cap, momentum, volatility, beta, quality, profitability, growth, leverage, liquidity, dividend yield, currency factors, ESG, crowding, sentiment, machine-learned factors, blended factor, RawValue, collinearity, estimation universe, look-ahead bias, point-in-time, exposure dynamics, factor admission criteria, t-statistic, non-redundancy, coverage, MiniModel, AXIOM, GUARDIAN

## Links
- [It's Just Beta (Home)](https://www.itsjustbeta.com/)
- [About](https://www.itsjustbeta.com/about/)
- [01 Why Factor Models Exist](https://www.itsjustbeta.com/chapters/01-introduction/)
- [02 The Factor Model Equation](https://www.itsjustbeta.com/chapters/02-the-factor-model-equation/)
- [03 Factors and Exposures](https://www.itsjustbeta.com/chapters/03-factors-and-exposures/)
- [04 Types of Factor Model](https://www.itsjustbeta.com/chapters/04-model-types/)
- [05 Estimation Universe and Coverage Universe](https://www.itsjustbeta.com/chapters/05-universes/)
- [06 Estimating Factor Returns: The Cross-Sectional Regression](https://www.itsjustbeta.com/chapters/06-estimating-factor-returns/)
- [07 Factor Portfolios](https://www.itsjustbeta.com/chapters/07-factor-portfolios/)
- [08 Risk Model Assembly: Factor Covariance, Specific Risk, and the Full Forecast](https://www.itsjustbeta.com/chapters/08-risk-model-assembly/)
- [09 Risk Attribution: Where Does My Risk Come From?](https://www.itsjustbeta.com/chapters/09-risk-attribution/)
- [10 Performance Attribution: Where Did My Returns Come From?](https://www.itsjustbeta.com/chapters/10-performance-attribution/)
- [11 Portfolio Construction: How Do I Build the Portfolio I Want?](https://www.itsjustbeta.com/chapters/11-portfolio-construction/)
- [12 Hedging: How Do I Remove the Risk I Don't Want?](https://www.itsjustbeta.com/chapters/12-hedging/)
- [13 Alpha Research: What Can't the Model Explain?](https://www.itsjustbeta.com/chapters/13-alpha-research/)
- [14 Evaluating a Factor Model: Is It Fit for Purpose?](https://www.itsjustbeta.com/chapters/14-model-evaluation/)
- [15 Modifying a Factor Model: Adding, Removing, and Changing Factors](https://www.itsjustbeta.com/chapters/15-modifying-the-model/)
- [16 Practical Considerations: Data, Implementation, and Pitfalls](https://www.itsjustbeta.com/chapters/16-practical-considerations/)
- [17 Appendix: Reference Material](https://www.itsjustbeta.com/chapters/17-appendix/)
- [18 Mini Example Source Code](https://www.itsjustbeta.com/chapters/18-mini-example-source-code/)
- [← Part 02 The Factor Model Equation (prev)](https://www.itsjustbeta.com/chapters/02-the-factor-model-equation/)
- [Part 04 → Types of Factor Model (next)](https://www.itsjustbeta.com/chapters/04-model-types/)
- External references:
  - [GICS (Global Industry Classification Standard) — Wikipedia](https://en.wikipedia.org/wiki/Global_Industry_Classification_Standard)
  - [ICB (Industry Classification Benchmark) — Wikipedia](https://en.wikipedia.org/wiki/Industry_Classification_Benchmark)
