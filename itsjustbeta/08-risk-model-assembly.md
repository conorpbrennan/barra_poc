# Risk Model Assembly: Factor Covariance, Specific Risk, and the Full Forecast
URL: https://www.itsjustbeta.com/chapters/08-risk-model-assembly/

## Summary

How historical factor returns and residuals become **forward-looking risk forecasts**. Three components: the **factor covariance matrix F**, the **specific risk matrix Δ** (delta), and the assembled asset covariance forecast **Σ = XFX' + Δ**.

### Factor covariance matrix (F)
The naive sample covariance treats all history equally — wrong, because **volatility clusters** and evolves. Standard fix: **EWMA (exponentially weighted moving average)** with decay parameter **λ**, usually quoted as a **half-life h** (the lag at which weights fall by 50%). Short half-lives (20–60 days) suit trading models; long ones (24–48 months) suit strategic allocation. Production refinements:
- **Separate dynamics for volatilities and correlations** — short half-life vols, long half-life correlations, recombined as **Dσ C Dσ**.
- **Newey–West adjustment** — corrects for serial correlation in factor returns when scaling across horizons.
- **Shrinkage and conditioning** (Ledoit–Wolf style) — pull extreme eigenvalues toward structured targets so an optimizer can't exploit them.
- **Regime adjustment** — multipliers calibrated to recent forecast errors, for rapid re-leveling in market transitions. (Related: GARCH-family volatility modeling.)

### Specific risk (Δ)
Idiosyncratic risk is hardest where history is sparse. Production models **blend two estimators**:
1. **Time-series**: EWMA of the stock's own residuals — good for liquid names with clean histories.
2. **Structural (cross-sectional)**: regression predicting specific volatility from current characteristics (size, liquidity, leverage, industry) — available for any stock, including IPOs.

Blend by **credibility weights γᵢ** that grow with history quality:
**σ̂²ϵᵢ = γᵢ·σ̂²TS,i + (1−γᵢ)·σ̂²STR,i** — coverage-universe assets are the limiting case **γᵢ = 0**.

### Assembled model & mini example
**Σ = XFX' + Δ** prices any portfolio's risk. MiniModel numbers:
- Portfolio volatility **17.55%** (factor 15.99%, specific 7.22%)
- Benchmark volatility **18.14%**
- **Active (tracking) risk 5.42%**
Insight: total risk is dominated by systematic (factor) risk, while **active risk splits more evenly** between factor and specific components (portfolio–benchmark diversification).

### Forecast validation
- **Bias statistic**: **b = std(rₚ,ₜ / σ̂ₚ,ₜ₋₁)** — realized return standardized by prior forecast vol. Calibrated model: b ≈ 1; **b > 1 = underforecast**, **b < 1 = overforecast**. 95% acceptance band ≈ **1 ± √(2/T)**.
- Also: **Q-statistics**, **exceedance counts** (how often returns breach the predicted 2σ band), and **segment breakdowns** to catch structural biases.

Known failure modes:
- **Underforecasting after calm periods** (EWMA forgets prior volatility)
- **Correlation spikes in crises** beyond any half-life's expectation
- **Optimizer bias** — exploitation of too-small eigenvalues

### Horizon variants
Short-horizon models (daily data, weekly half-lives) respond in days — trading. Long-horizon models (monthly data, 1–4 year half-lives) stabilize over quarters — strategic allocation. A mismatched horizon either chases noise or lags reality.

## Key terms
risk model assembly, factor covariance matrix, F, specific risk, Delta, asset covariance, Sigma = XFX' + Delta, EWMA, exponentially weighted moving average, decay parameter lambda, half-life, volatility clustering, volatility and correlation dynamics, D sigma C D sigma, Newey-West adjustment, serial correlation, horizon scaling, shrinkage, conditioning, eigenvalues, Ledoit-Wolf, regime adjustment, GARCH, time-series specific risk, structural specific risk, cross-sectional volatility regression, credibility weight gamma, blend, IPO, coverage universe gamma = 0, portfolio volatility, benchmark volatility, active risk, tracking error, bias statistic, b = std(r/sigma), acceptance band 1 +/- sqrt(2/T), Q-statistic, exceedance count, 2 sigma band, underforecast, overforecast, optimizer bias, short-horizon model, long-horizon model, MiniModel

## Links
- [It's Just Beta home](https://www.itsjustbeta.com/)
- [About](https://www.itsjustbeta.com/about/)
- [01 Why Factor Models Exist](https://www.itsjustbeta.com/chapters/01-introduction/)
- [02 The Factor Model Equation](https://www.itsjustbeta.com/chapters/02-the-factor-model-equation/)
- [03 Factors and Exposures](https://www.itsjustbeta.com/chapters/03-factors-and-exposures/)
- [04 Types of Factor Model](https://www.itsjustbeta.com/chapters/04-model-types/)
- [05 Estimation Universe and Coverage Universe](https://www.itsjustbeta.com/chapters/05-universes/)
- [06 Estimating Factor Returns: The Cross-Sectional Regression](https://www.itsjustbeta.com/chapters/06-estimating-factor-returns/)
- [07 Factor Portfolios](https://www.itsjustbeta.com/chapters/07-factor-portfolios/)
- [08 Risk Model Assembly (this page)](https://www.itsjustbeta.com/chapters/08-risk-model-assembly/)
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
- [← Part 07 Factor Portfolios (prev)](https://www.itsjustbeta.com/chapters/07-factor-portfolios/)
- [Part 09 Risk Attribution → (next)](https://www.itsjustbeta.com/chapters/09-risk-attribution/)
- [Newey–West estimator (Wikipedia)](https://en.wikipedia.org/wiki/Newey%E2%80%93West_estimator)
- [Autoregressive conditional heteroskedasticity / GARCH (Wikipedia)](https://en.wikipedia.org/wiki/Autoregressive_conditional_heteroskedasticity)
- [Estimation of covariance matrices / Ledoit–Wolf (Wikipedia)](https://en.wikipedia.org/wiki/Estimation_of_covariance_matrices)
- [Feedback contact: chris (at) itsjustbeta.com](mailto:chris@itsjustbeta.com)
