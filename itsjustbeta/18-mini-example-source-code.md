# Mini Example Source Code
URL: https://www.itsjustbeta.com/chapters/18-mini-example-source-code/

## Summary
Appendix chapter (Part 18): a complete, self-contained, executable Python script — **`mini_example.py`** — that reproduces every numerical example cited across the primer's chapters, on the 10-stock mini-example dataset defined in Chapter 17 (§17.5). Purpose: transparency and reproducibility — every figure quoted in the text can be recomputed by running one file, with numerical verification/tie-out checks embedded throughout.

### Structure of the script (nine computational sections)
1. **Universe definition.** The 10-stock universe with industry classifications (Tech/Finance/Consumer), market caps, style descriptors (book-to-price, 12-1 momentum, size), realized returns, and risk characteristics; defines both **manager (portfolio)** and **benchmark** weight vectors.
2. **Standardized exposures.** Builds the exposure matrix **X** with seven factors: Market intercept, three industry dummies (TECH, FIN, CONS), plus Value, Momentum, and Size styles. Standardization convention: **cap-weighted mean zero, equal-weighted standard deviation one** (Z-scores).
3. **Constrained WLS cross-sectional regression.** Weighted least squares with an **industry constraint** — cap-weighted industry (sector) factor returns sum to zero — estimating month-1 factor returns and the **pure factor portfolios** P = (XᵀWX)⁻¹XᵀW.
4. **Risk model assembly.** Builds the factor covariance matrix F from factor volatilities and correlations plus diagonal specific risk Δ; decomposes portfolio risk into factor and idiosyncratic components with **marginal contributions** and **contribution-to-risk** analysis (Euler decomposition).
5. **Stress testing.** Conditional factor propagation under a **two-sigma decline in the Value factor** — contrasting the naive (Value-only) impact with the correlated (all-factors-move) portfolio impact.
6. **Multi-period attribution.** Three-month performance attribution using **Carino linking coefficients** to scale per-period factor contributions so they sum to the compounded (geometric) return.
7. **Hedging.** Two-instrument hedge (index future + small-cap future) neutralizing Market and Size exposures; also the minimum-variance single-instrument hedge (h* = −β).
8. **Portfolio optimization.** Constrained optimization: **minimize tracking error** while holding the current Value exposure and **neutralizing Momentum**.
9. **Chapter 2 validation.** A hand-checkable five-stock, two-factor toy example verifying the core factor-model-equation calculations.

### Key concepts / definitions used in the code
- **Exposures (X):** factor sensitivities standardized to cap-weighted mean 0, equal-weighted std 1.
- **Pure factor portfolios (P):** zero-cost portfolios with unit exposure to one factor, zero to all others (PX = I).
- **Risk decomposition:** portfolio variance split into systematic (factor, wᵀXFXᵀw) and idiosyncratic (specific, wᵀΔw) parts.
- **Active positions:** portfolio weights minus benchmark weights; **tracking error** = risk of the active portfolio.
- **Cross-sectional regression:** estimating factor returns by regressing stock returns on lagged exposures.
- **Constraint enforcement:** keeping economic/accounting constraints (Cf = 0) inside the regression and the optimizer.

### Takeaway
Factor models give one structured, reproducible framework for risk decomposition, performance attribution, stress testing, hedging, and portfolio construction — and the entire primer's arithmetic fits in one transparent script.

## Key terms
mini_example.py, mini example source code, Python script, reproducible, 10-stock universe, universe definition, standardized exposures, exposure matrix X, Z-scores, cap-weighted mean zero, equal-weighted standard deviation, Market intercept, industry dummies, Value, Momentum, Size, constrained WLS, cross-sectional regression, industry constraint, factor returns, pure factor portfolios, risk model assembly, factor covariance, specific risk, marginal contribution, contribution to risk, stress testing, two-sigma Value shock, conditional factor propagation, multi-period attribution, Carino linking, hedging, index future, small-cap future, minimum-variance hedge, portfolio optimization, tracking error minimization, momentum neutralization, active positions, benchmark weights, five-stock two-factor validation, Chapter 2 validation

## Links
- [Home / Chapters root](https://www.itsjustbeta.com/)
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
- [18 Mini Example Source Code](https://www.itsjustbeta.com/chapters/18-mini-example-source-code/) (current page)
- [← Previous: Part 17 Appendix: Reference Material](https://www.itsjustbeta.com/chapters/17-appendix/) (last chapter — no next link)
- [Feedback: chris@itsjustbeta.com](mailto:chris@itsjustbeta.com) (rendered obfuscated on-page as "chrisitsjustbeta.com")
