# Appendix: Reference Material
URL: https://www.itsjustbeta.com/chapters/17-appendix/

## Summary
Part 17 — the primer's reference appendix: notation summary, least-squares family reference, derivation collection, glossary, the complete mini-example dataset, and an annotated bibliography.

### 17.1 Notation summary
30+ symbols used throughout the primer:
- **Dimensions:** N = number of assets, K = number of factors, T = number of time periods.
- **Core objects:** r = asset returns vector, X = exposure (loadings) matrix (N×K), f = factor returns vector, ε = specific (idiosyncratic) returns.
- **Covariance structures:** F = factor covariance matrix (K×K), Δ = specific-variance (diagonal) matrix, Σ = asset covariance matrix.
- **Portfolio objects:** w = weights vector, x = portfolio factor exposures (x = Xᵀw).
- **Regression objects:** H = projection matrix, C = constraint matrix, R = restriction matrix, W = diagonal regression weight matrix.
- **Central identity:** **Σ = XFXᵀ + Δ** — total asset covariance = explained (factor-driven) + unexplained (specific) components.

### 17.2 Least-squares family reference
- **OLS:** f̂ = (XᵀX)⁻¹Xᵀr
- **WLS:** f̂ = (XᵀWX)⁻¹XᵀWr, W diagonal (e.g. cap or inverse-specific-variance weights)
- **GLS:** f̂ = (XᵀΩ⁻¹X)⁻¹XᵀΩ⁻¹r, Ω a general error covariance
- **EWMA covariance:** recursive with decay λ; effective sample size ≈ (1+λ)/(1−λ)
- **Eigendecomposition / random-matrix denoising:** Marchenko–Pastur noise edge λ± = σ²(1 ± √(N/T))²

### 17.3 Derivation collection (six derivations)
- **D1 — Constrained WLS:** minimize S(f) = (r−Xf)ᵀW(r−Xf) subject to Cf = 0; the bordered (KKT) system yields f̂ and Lagrange multipliers λ that price the constraints.
- **D2 — Pure factor portfolios:** row k of P = (XᵀWX)⁻¹XᵀW has unit exposure to factor k and zero to all others; PX = I.
- **D3 — Euler risk decomposition:** σ(w) = Σᵢ wᵢ(Σw)ᵢ/σ — portfolio volatility decomposes exactly into per-asset contributions; the factor-space version uses factor exposures.
- **D4 — Characteristic portfolio:** minimum-variance portfolio with unit exposure to characteristic x: h_x = Σ⁻¹x/(xᵀΣ⁻¹x), variance 1/(xᵀΣ⁻¹x).
- **D5 — Woodbury identity:** Σ⁻¹ = Δ⁻¹ − Δ⁻¹X(F⁻¹ + XᵀΔ⁻¹X)⁻¹XᵀΔ⁻¹ — cuts inversion cost from O(N³) to O(NK² + K³).
- **D6 — Minimum-variance hedge:** multi-instrument h* = −Cov(r_H)⁻¹Cov(r_H, r_p); single-instrument h* = −β_{p,h}.

### 17.4 Glossary (~30 terms)
Notable entries: **active return/risk** (portfolio minus benchmark); **alpha** ("expected return not explained by factor exposures"); **bias statistic** ("std of realized returns standardized by forecast volatility"); **characteristic portfolio** (minimum-variance unit-exposure portfolio); **idiosyncratic risk** ("return variance unique to a stock. Diagonal of Δ. Diversifiable."); **information coefficient (IC)** (cross-sectional correlation between signal and forward returns); **VIF** (variance inflation factor, redundancy gauge = 1/(1−R²)); **winsorization** (clipping extreme values before standardization).

### 17.5 Mini example dataset
The complete 10-stock quarterly dataset used in every chapter's numerical examples:
- **Universe:** 10 stocks — 4 tech, 3 finance, 3 consumer; market caps $10bn–$150bn.
- **Style descriptors:** book-to-price (B/P), 12-1 momentum, specific volatility; industries Tech / Finance / Consumer with the constraint that cap-weighted industry factor returns sum to zero.
- **Factor covariance F:** 7×7 (MKT, TECH, FIN, CONS, VALUE, MOM, SIZE) built from specified factor volatilities and correlations, verified PSD.
- **Later months:** factor returns f₂ and f₃ specified; active specific returns +0.30% and −0.10%.
- **Hedge instruments:** an index future and a small-cap future with specified factor exposures.
- **Key results:** portfolio volatility 17.55%, active volatility (tracking error) 5.42%; quarterly attribution VALUE +0.46%, MOM −0.72%, specific +0.24%.

### 17.6 Annotated bibliography
- **Foundations:** Sharpe (1964, single-factor/CAPM origin); Ross (1976, APT multi-factor justification); Fama & MacBeth (1973, two-pass cross-sectional methodology); Chen, Roll & Ross (1986, macroeconomic factors); Fama & French (1992, 1993, 2015) and Carhart (1997, style factors/momentum); Rosenberg (1974, foundational cross-sectional/Barra architecture).
- **Books:** Grinold & Kahn (practitioner fundamentals, Active Portfolio Management); Connor, Goldberg & Korajczyk (rigorous treatment); Qian, Hua & Sorensen (modeling and construction); Litterman et al. (risk decomposition).
- **Methods:** Ledoit & Wolf (shrinkage covariance); Newey & West (autocorrelation-consistent estimation); Shanken (errors-in-variables correction); Menchero, Carino (multi-period attribution / Carino linking); Black & Litterman (equilibrium returns); Harvey, Liu & Zhu and Hou, Xue & Zhang (factor validation/replication); Kelly, Pruitt & Su (IPCA) and Gu, Kelly & Xiu (machine learning asset pricing).
- **Practitioner:** MSCI Barra model handbooks; Axioma/SimCorp implementation papers; Menchero, Orr & Wang (bridge references).

## Key terms
notation summary, N assets, K factors, T periods, exposure matrix X, factor returns f, specific returns epsilon, factor covariance F, specific variance Delta, asset covariance Sigma, Sigma = XFX' + Delta, projection matrix, constraint matrix, OLS, WLS, GLS, EWMA, decay lambda, effective sample size, eigendecomposition, Marchenko-Pastur, constrained WLS, Lagrange multipliers, bordered system, pure factor portfolios, PX = I, Euler risk decomposition, risk contributions, characteristic portfolio, Woodbury identity, minimum-variance hedge, hedge ratio, beta, glossary, active return, active risk, alpha, bias statistic, idiosyncratic risk, information coefficient, IC, VIF, variance inflation factor, winsorization, mini example dataset, 10-stock universe, book-to-price, 12-1 momentum, specific volatility, tracking error 5.42%, portfolio volatility 17.55%, Carino linking, annotated bibliography, Sharpe, Ross APT, Fama MacBeth, Fama French, Carhart, Rosenberg, Grinold Kahn, Ledoit Wolf shrinkage, Newey West, Shanken, Black Litterman, IPCA, Gu Kelly Xiu, MSCI Barra handbooks, Axioma

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
- [11 Portfolio Construction: How Do I Build the Portfolio I Want?](https://www.itsjustbeta.com/chapters/11-portfolio-construction/) (marked "soon")
- [12 Hedging: How Do I Remove the Risk I Don't Want?](https://www.itsjustbeta.com/chapters/12-hedging/) (marked "soon")
- [13 Alpha Research: What Can't the Model Explain?](https://www.itsjustbeta.com/chapters/13-alpha-research/) (marked "soon")
- [14 Evaluating a Factor Model: Is It Fit for Purpose?](https://www.itsjustbeta.com/chapters/14-model-evaluation/) (marked "soon")
- [15 Modifying a Factor Model: Adding, Removing, and Changing Factors](https://www.itsjustbeta.com/chapters/15-modifying-the-model/) (marked "soon")
- [16 Practical Considerations: Data, Implementation, and Pitfalls](https://www.itsjustbeta.com/chapters/16-practical-considerations/)
- [18 Mini Example Source Code](https://www.itsjustbeta.com/chapters/18-mini-example-source-code/)
- [← Previous: Part 16 Practical Considerations: Data, Implementation, and Pitfalls](https://www.itsjustbeta.com/chapters/16-practical-considerations/)
- [Next: Appendix — Mini Example Source Code →](https://www.itsjustbeta.com/chapters/18-mini-example-source-code/)
- [Marchenko–Pastur distribution (Wikipedia)](https://en.wikipedia.org/wiki/Marchenko%E2%80%93Pastur_distribution) — external reference in §17.2
- [Feedback: chris@itsjustbeta.com](mailto:chris@itsjustbeta.com) (rendered obfuscated on-page as "chrisitsjustbeta.com")
