# Types of Factor Model
URL: https://www.itsjustbeta.com/chapters/04-model-types/

## Summary
Chapter 4 classifies factor models by which components are **observed** versus **estimated**. All models produce the same four ingredients: exposures (X), factor returns (f), factor covariance (F), and specific risk (Δ).

**Time-series / macroeconomic models.** Observable factor series (market returns, inflation surprises, yield curve shifts) are the inputs. Stock sensitivities (β) are estimated by a per-asset time-series regression:

    r_it = α_i + β_i⊤ f_t + ϵ_it

Challenges: **stale exposures** (slow to reflect company changes), **poor single-stock explanatory power** (R² < 20%), and inability to model new listings (no return history). They survive in macro scenario analysis and rate/oil sensitivity overlays.

**Cross-sectional / fundamental models** reverse the approach: exposures are **measured** from observable characteristics each period; factor returns are **recovered** via cross-sectional regression:

    r_t = X_{t−1} f_t + ϵ_t

This architecture dominates commercial risk models (**MSCI Barra**, **SimCorp Axioma**). Advantages: fast reaction to fundamental changes, high explanatory power (20–40% R² per month). Disadvantages: extensive data infrastructure requirements, potential blindness to unmeasured drivers.

**Statistical models** extract factors via **Principal Component Analysis (PCA)** of returns alone — no characteristics needed. The eigendecomposition:

    Σ̂ = QΛQ⊤

yields exposure vectors from eigenvectors. Choosing the number of factors uses **scree plots**, the **Marchenko–Pastur law** (noise eigenvalues bounded in [(1−√(N/T))², (1+√(N/T))²]σ²), or **asymptotic PCA** when N ≫ T. Tradeoff: captures unnamed structure but sacrifices interpretability — eigenvectors have no stable names (**rotation indeterminacy**). Most useful for statistical arbitrage and diagnosing fundamental-model blind spots.

**Hybrid models** combine a fundamental core with statistical factors extracted from the residuals — balancing interpretability against robustness to missing factors.

**Comparison / when to use which:** fundamental models excel at institutional reporting and attribution (interpretable, responsive); statistical models suit short-horizon trading and diagnostics (fastest reaction, interpretability irrelevant); macro models address macroeconomic questions directly.

## Key terms
model types, time-series model, macroeconomic model, cross-sectional model, fundamental model, statistical model, hybrid model, observed vs estimated, exposures X, factor returns f, factor covariance F, specific risk Δ, time-series regression, r_it = α_i + β_i⊤ f_t + ϵ_it, stale exposures, R², cross-sectional regression, r_t = X_{t−1} f_t + ϵ_t, MSCI Barra, SimCorp Axioma, commercial risk models, PCA, principal component analysis, eigendecomposition, Σ̂ = QΛQ⊤, eigenvectors, eigenvalues, scree plot, Marchenko–Pastur law, random matrix theory, asymptotic PCA, rotation indeterminacy, statistical arbitrage, blind spots, macro scenario analysis, inflation surprises, yield curve, new listings, interpretability

## Links
- [Home](https://www.itsjustbeta.com/)
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
- [← Part 03 Factors and Exposures (prev)](https://www.itsjustbeta.com/chapters/03-factors-and-exposures/)
- [Part 05 → Estimation Universe and Coverage Universe (next)](https://www.itsjustbeta.com/chapters/05-universes/)
- External references:
  - [Beta (finance) — Wikipedia (Improved estimators)](https://en.wikipedia.org/wiki/Beta_%28finance%29#Improved_estimators)
  - [Marchenko–Pastur distribution — Wikipedia](https://en.wikipedia.org/wiki/Marchenko%E2%80%93Pastur_distribution)
- [chris@itsjustbeta.com (contact)](#)
