# Estimating Factor Returns: The Cross-Sectional Regression
URL: https://www.itsjustbeta.com/chapters/06-estimating-factor-returns/

## Summary

The mathematical core of the series: each period, one **cross-sectional regression** turns measured factor exposures into factor returns. Stocks are the observations (N data points), factors are the coefficients (K unknowns).

### Core equation
**r = Xf + ε**
- **r** — N×1 vector of realized stock returns
- **X** — N×K factor exposure matrix, known at period start
- **f** — K×1 vector of factor returns (unknown, estimated)
- **ε** — N×1 vector of specific / idiosyncratic returns

### Estimation methods
- **OLS (ordinary least squares)**: minimizes the sum of squared residuals; solution **f̂ = (X'X)⁻¹X'r**. Geometrically, projects the return vector r onto the K-dimensional space spanned by the factor exposures; residuals are perpendicular to that surface.
- **WLS (weighted least squares)**: specific variance differs hugely across stocks — micro-caps have far noisier residuals than mega-caps (heteroskedasticity). Solution **f̂ = (X'WX)⁻¹X'Wr** with diagonal weight matrix W. Standard practical weight: wᵢ ∝ **√capᵢ** (square root of market capitalization), a balance between equal weighting and full cap weighting. (Gauss–Markov motivates the weighting.)

### Multicollinearity and constraints
Market exposure and industry dummies are **exactly collinear** — the market column equals the sum of all industry columns — so X'WX is **singular**. Fix: impose one identifying **constraint** per degeneracy. Standard constraint: the **cap-weighted average of industry factor returns equals zero**, Σⱼ cⱼfⱼ = 0. This allocates common movement to the market factor; each industry factor is then that sector's return *relative to the market*. Rather than dropping a column (a silent reference category), use a K×(K−1) **restriction matrix R** parameterizing the feasible space: **ĝ = (R'X'WXR)⁻¹R'X'Wr**, **f̂ = Rĝ**.

### Pure factor portfolios duality
**f̂ = Pr**, where P is a K×N matrix whose rows are investable **long-short portfolios**. Each factor return is literally the return of a portfolio with unit exposure to its own factor and zero to all others — factor returns and factor portfolios are two views of one object (developed in Chapter 07).

### Robustness refinements
- **Return outliers** (takeovers, bankruptcies) distort estimates → **winsorization** or **Huber-weighted (robust) regression**.
- **Thin factors**: industries with few members give factor returns dominated by idiosyncratic moves → coarser classification, **Bayesian shrinkage** toward parent sectors, or minimum-membership rules.
- **Advanced heteroskedasticity**: with estimated specific variances, a second pass with W = Δ̂⁻¹ gives **GLS (generalized least squares)** precision.

### Diagnostics
- **Cross-sectional R²**: 0.2–0.4 is typical and healthy for monthly single-stock data (most monthly single-stock movement is specific). Trends matter more than levels.
- **Factor t-statistics**: single-period tₖ tests whether a factor return differs from zero that month; more meaningful is the **fraction of periods with |tₖ| > 2** — factors clearing that bar in ≥ one-third of periods justify inclusion.
- **Residual structure**: correlated residuals by theme, ownership, or supply chain signal **missing factors**.

### Worked example (MiniModel month 1)
10 stocks, 7 factors, 1 constraint:
- Market return +1.821% (matches the actual cap-weighted return — validates the constraint)
- Tech beat the market: +0.768%; Financials lagged: −1.282%
- Momentum strong: +1.962% per σ
- Weighted R² = 0.956 (artificially high on the small sample; production sees 0.2–0.4)
- Residuals expose stock-specific moves (INDIGO +1.09% above prediction)

## Key terms
cross-sectional regression, factor returns, factor exposures, exposure matrix X, specific returns, idiosyncratic returns, residuals, OLS, ordinary least squares, WLS, weighted least squares, GLS, generalized least squares, heteroskedasticity, square-root-of-cap weighting, Gauss–Markov theorem, multicollinearity, singular matrix, identifying constraint, cap-weighted industry constraint, restriction matrix, reference category, pure factor portfolios, f = Pr, long-short portfolio, winsorization, Huber loss, robust regression, thin factors, Bayesian shrinkage, minimum membership, cross-sectional R-squared, t-statistic, residual structure, missing factors, MiniModel

## Links
- [Home](https://www.itsjustbeta.com/)
- [About](https://www.itsjustbeta.com/about/)
- [01 Why Factor Models Exist](https://www.itsjustbeta.com/chapters/01-introduction/)
- [02 The Factor Model Equation](https://www.itsjustbeta.com/chapters/02-the-factor-model-equation/)
- [03 Factors and Exposures](https://www.itsjustbeta.com/chapters/03-factors-and-exposures/)
- [04 Types of Factor Model](https://www.itsjustbeta.com/chapters/04-model-types/)
- [05 Estimation Universe and Coverage Universe](https://www.itsjustbeta.com/chapters/05-universes/)
- [06 Estimating Factor Returns: The Cross-Sectional Regression (this page)](https://www.itsjustbeta.com/chapters/06-estimating-factor-returns/)
- [07 Factor Portfolios](https://www.itsjustbeta.com/chapters/07-factor-portfolios/)
- [08 Risk Model Assembly](https://www.itsjustbeta.com/chapters/08-risk-model-assembly/)
- [09 Risk Attribution](https://www.itsjustbeta.com/chapters/09-risk-attribution/)
- [10 Performance Attribution](https://www.itsjustbeta.com/chapters/10-performance-attribution/)
- [11 Portfolio Construction](https://www.itsjustbeta.com/chapters/11-portfolio-construction/)
- [12 Hedging](https://www.itsjustbeta.com/chapters/12-hedging/)
- [13 Alpha Research](https://www.itsjustbeta.com/chapters/13-alpha-research/)
- [14 Evaluating a Factor Model](https://www.itsjustbeta.com/chapters/14-model-evaluation/)
- [15 Modifying a Factor Model](https://www.itsjustbeta.com/chapters/15-modifying-the-model/)
- [16 Practical Considerations](https://www.itsjustbeta.com/chapters/16-practical-considerations/)
- [17 Appendix: Reference Material](https://www.itsjustbeta.com/chapters/17-appendix/)
- [18 Mini Example Source Code](https://www.itsjustbeta.com/chapters/18-mini-example-source-code/)
- [← Part 05 Estimation Universe and Coverage Universe (prev)](https://www.itsjustbeta.com/chapters/05-universes/)
- [Part 07 Factor Portfolios → (next)](https://www.itsjustbeta.com/chapters/07-factor-portfolios/)
- [Gauss–Markov theorem (Wikipedia)](https://en.wikipedia.org/wiki/Gauss%E2%80%93Markov_theorem)
- [Huber loss (Wikipedia)](https://en.wikipedia.org/wiki/Huber_loss)
- [Shrinkage (statistics) (Wikipedia)](https://en.wikipedia.org/wiki/Shrinkage_(statistics))
- [Feedback contact: chris (at) itsjustbeta.com](mailto:chris@itsjustbeta.com)
