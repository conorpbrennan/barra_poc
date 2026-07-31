# Factor Portfolios
URL: https://www.itsjustbeta.com/chapters/07-factor-portfolios/

## Summary

How the regression coefficients of Chapter 06 translate into **investable portfolios**. Core insight: estimating factor returns and constructing factor portfolios are the **same operation** — f̂ = Pr, each factor return is literally a portfolio return.

### Pure factor portfolios
Row k of the matrix **P** is a portfolio with **unit exposure to factor k and zero exposure to every other factor**; the purity is guaranteed by the fundamental property **PX = I**. The factor return equals that portfolio's realized return — *"the cleanest available realization of what value did, holding everything else constant."*

### Construction methods
- **Regression approach**: **P = (X'WX)⁻¹X'W**, W = regression weights proportional to market capitalization (the Chapter-06 WLS dual).
- **Characteristic portfolio approach**: minimum-variance optimization subject to unit-exposure constraints; weights proportional to **Σ⁻¹x**.

### The purity–leverage tradeoff
Pure portfolios need significant **leverage** and **short-selling** to keep zero cross-factor exposures. MiniModel example: the VALUE pure portfolio needs **11.9x gross leverage**, driven by factor collinearity with the industry classifications.

### Practical frictions
- High leverage, short positions, hard-to-borrow names (liquidity constraints)
- **Turnover**: momentum portfolios turn over several hundred percent annually
- **Trading costs**: a substantial drag on gross returns
- **Crowding**: many participants using identical models

### Tradable factor portfolios
Constrained versions that **sacrifice purity for implementability**: position limits, turnover caps, long-only constraints. They reintroduce incidental exposures but are actually investable.

### Academic construction — sorted portfolios
**Fama-French** factors use ranking + cap-weighted bucketing (sorts), not regression. Simpler construction (~2x leverage) but carries **incidental exposures** to industries and size. Historically motivated by computational constraints.

### Fama-MacBeth procedure
Three steps, testing whether characteristics carry **risk premiums**:
1. **First pass** — time-series regression estimating each stock's exposures.
2. **Second pass** — cross-sectional regression each period, generating a premium estimate per period.
3. **Inference** — mean and standard error of the premium estimates across time.
It works because period-by-period estimates are nearly **serially uncorrelated**, so time-series variability is the right uncertainty measure.

## Key terms
factor portfolios, pure factor portfolio, PX = I, f = Pr, unit exposure, zero cross-factor exposure, regression approach, P = (X'WX)^-1 X'W, characteristic portfolio, minimum variance, Sigma^-1 x, purity-leverage tradeoff, gross leverage, short selling, long-short, turnover, trading costs, hard to borrow, liquidity constraints, crowding, tradable factor portfolios, position limits, turnover caps, long-only, incidental exposures, sorted portfolios, Fama-French, cap-weighted bucketing, Fama-MacBeth, risk premium, two-pass regression, serially uncorrelated, standard error, MiniModel, VALUE portfolio 11.9x leverage

## Links
- [Home / Index](https://www.itsjustbeta.com/)
- [About](https://www.itsjustbeta.com/about/)
- [01 Why Factor Models Exist](https://www.itsjustbeta.com/chapters/01-introduction/)
- [02 The Factor Model Equation](https://www.itsjustbeta.com/chapters/02-the-factor-model-equation/)
- [03 Factors and Exposures](https://www.itsjustbeta.com/chapters/03-factors-and-exposures/)
- [04 Types of Factor Model](https://www.itsjustbeta.com/chapters/04-model-types/)
- [05 Estimation Universe and Coverage Universe](https://www.itsjustbeta.com/chapters/05-universes/)
- [06 Estimating Factor Returns](https://www.itsjustbeta.com/chapters/06-estimating-factor-returns/)
- [07 Factor Portfolios (this page)](https://www.itsjustbeta.com/chapters/07-factor-portfolios/)
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
- [← Part 06 Estimating Factor Returns (prev)](https://www.itsjustbeta.com/chapters/06-estimating-factor-returns/)
- [Part 08 Risk Model Assembly → (next)](https://www.itsjustbeta.com/chapters/08-risk-model-assembly/)
- [Feedback contact: chris (at) itsjustbeta.com](mailto:chris@itsjustbeta.com)
