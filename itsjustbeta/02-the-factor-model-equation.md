# The Factor Model Equation
URL: https://www.itsjustbeta.com/chapters/02-the-factor-model-equation/

## Summary
Chapter 2 presents the foundational mathematics of factor models: decomposing asset returns into systematic (factor-driven) and idiosyncratic components.

**The fundamental equation.** For N stocks and K factors over one period:

    r = Xf + ϵ

where:
- **r** (N×1) — asset returns over the period
- **X** (N×K) — factor exposure / loading matrix
- **f** (K×1) — factor returns over the period
- **ϵ** (N×1) — specific / idiosyncratic returns

Each stock's return is a weighted sum of its factor exposures times the corresponding factor returns, plus an unexplained residual. Illustrated with the Mini-Example stock AXIOM earning a 4.30% systematic return across seven factors.

**Three critical assumptions:**
1. **A1 — zero-mean specific returns:** E[ϵᵢ] = 0. All systematic payoffs belong in factors; specific returns are surprises.
2. **A2 — factor–specific orthogonality:** Cov(fₖ, ϵᵢ) = 0. No leakage between systematic and idiosyncratic components; guaranteed by construction under least-squares regression.
3. **A3 — cross-sectional orthogonality:** Cov(ϵᵢ, ϵⱼ) = 0 for i ≠ j. The "key assumption" — factors capture all common movement. Failures occur with linked securities or missing factors; both are addressable via explicit treatment or new factor discovery.

**The covariance decomposition.** Taking covariances of both sides:

    Σ = XFX' + Δ

where:
- **Σ** (N×N) — asset covariance matrix
- **F** (K×K) — factor covariance matrix
- **Δ** (N×N, diagonal) — specific variance matrix

This reduces estimation from N(N+1)/2 covariances to K(K+1)/2 factor covariances + NK exposures + N specific variances. At institutional scale (3,000 stocks, 70 factors): 4.5 million parameters vs 215,485 — a 95% reduction. Since exposures come from observable characteristics rather than statistical estimation, the genuine statistical burden is roughly 5,485 parameters.

**Portfolio risk decomposition.** For a portfolio with weights w:

    σₚ² = xₚ'Fxₚ + w'Δw,   where xₚ = X'w

xₚ is the K×1 vector of portfolio factor exposures (weighted-average exposures across holdings). Two implications:
- **Specific risk diversifies:** the specific variance term involves squared weights and vanishes as N→∞; market exposure stays constant regardless of diversification.
- **Independent ledgers:** risk analysis separates into K-dimensional factor analysis and per-position specific tracking.

For active management, substitute **active weights wₐ = wₚ − wᵦ** (which sum to zero) to compute tracking error.

**Returns conventions** practitioners must standardize:
- **Arithmetic vs logarithmic:** models operate on arithmetic returns (they aggregate linearly across assets); log returns aggregate across time.
- **Total vs excess returns:** risk models typically use excess returns above the risk-free rate.
- **Currency perspectives:** multi-country models separate local and currency returns explicitly.
- **Frequency:** daily and monthly models are different models; annualization uses 252 trading days or 12 months for variance, square-root scaling for volatility.

**Worked example (five stocks, two factors — market and value, weights summing to 1.0):**
1. Portfolio exposures: xₚ = X'w = (1.0, 0.235)
2. Factor variance: xₚ'Fxₚ = 0.025087 (factor volatility 15.84%)
3. Specific variance: Σ wᵢ²σϵᵢ² = 0.011311 (specific volatility 10.64%)
4. Total risk: σₚ = √0.036398 ≈ 19.08% annually

Factor variance share is 68.9% — "most of the risk is factor risk, dominated by the market term."

## Key terms
factor model equation, r = Xf + ϵ, exposure matrix X, factor returns f, specific returns, idiosyncratic returns, residual, assumption A1 zero mean, assumption A2 factor-specific orthogonality, assumption A3 cross-sectional orthogonality, Cov(fk, ϵi) = 0, Cov(ϵi, ϵj) = 0, covariance decomposition, Σ = XFX' + Δ, factor covariance matrix F, specific variance matrix Δ, diagonal, parameter reduction, portfolio risk decomposition, σp² = xp'Fxp + w'Δw, xp = X'w, portfolio factor exposures, specific risk diversifies, active weights, tracking error, wa = wp − wb, arithmetic returns, log returns, excess returns, risk-free rate, currency returns, annualization, 252 trading days, factor variance share, AXIOM, worked example

## Links
- [It's Just Beta (Home / ITSJUSTBETA.COM)](https://www.itsjustbeta.com/)
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
- [← Part 01 Why Factor Models Exist (prev)](https://www.itsjustbeta.com/chapters/01-introduction/)
- [Part 03 → Factors and Exposures (next)](https://www.itsjustbeta.com/chapters/03-factors-and-exposures/)
- In-text cross-references:
  - [Mini Example (multiple references)](https://www.itsjustbeta.com/chapters/18-mini-example-source-code/)
  - [Chapter 3 — Factors and Exposures](https://www.itsjustbeta.com/chapters/03-factors-and-exposures/)
  - [Chapter 5 — Estimation Universe](https://www.itsjustbeta.com/chapters/05-universes/)
  - [Chapter 6 — Estimating Factor Returns](https://www.itsjustbeta.com/chapters/06-estimating-factor-returns/)
  - [Chapter 8 — Risk Model Assembly](https://www.itsjustbeta.com/chapters/08-risk-model-assembly/)
  - [Chapter 9 — Risk Attribution](https://www.itsjustbeta.com/chapters/09-risk-attribution/)
  - [Chapter 10 — Performance Attribution](https://www.itsjustbeta.com/chapters/10-performance-attribution/)
  - [Chapter 15 — Modifying the Model](https://www.itsjustbeta.com/chapters/15-modifying-the-model/)
  - [Chapter 16 — Practical Considerations](https://www.itsjustbeta.com/chapters/16-practical-considerations/)
- [chris@itsjustbeta.com (feedback email)](#)
