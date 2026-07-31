# Estimation Universe and Coverage Universe
URL: https://www.itsjustbeta.com/chapters/05-universes/

## Summary

Chapter 05 distinguishes the two universes every factor model carries:

- **Estimation universe** — the set of stocks used to estimate factor returns via the cross-sectional regression and to build the factor covariance matrix. Prioritizes **data quality**: liquid, well-measured names.
- **Coverage universe** — the (often order-of-magnitude larger) set of securities that receive model exposures and risk forecasts. Prioritizes **completeness**, so real portfolios can be risk-assessed.

Guiding principle: *"fit the factors on a clean core, then extend the model to everything else."*

### Designing an estimation universe (§5.2)
Membership criteria:
- **Liquidity and price quality**: minimum trading frequency, daily volume, free float.
- **Minimum size**: market-cap thresholds or percentile-based inclusion.
- **History requirements**: minimum trading history (1–3 months); primary listings only.
- **Representativeness**: enough members per industry and country for factor identifiability.
- **Stability buffers**: different entry vs exit thresholds (hysteresis) to reduce membership churn.
- **Event handling**: suspensions, M&A targets, delistings handled explicitly.
- **Point-in-time membership**: include failed assets as-of each date to avoid **survivorship bias**; look-ahead discipline matters as much as data quality.

### Weighting within the estimation universe (§5.3)
Stocks are weighted by the **square root of market cap** — large, well-measured stocks influence factor estimates more, but not in proportion to their extremely skewed caps.

### Extending the model to the coverage universe (§5.4)
- **Exposures**: apply the *identical* standardization — the estimation universe's mean μₖ and standard deviation σₖ — to coverage assets. Coverage names may legitimately have **out-of-range z-scores**.
- **Missing descriptors**: imputation ladder — peer-bucket mean → regression imputation → zero.
- **Specific risk** for coverage names, three approaches:
  1. **Peer/bucket assignment** — average specific risk within industry–size–country buckets.
  2. **Structural models** — cross-sectional regression predicting specific variance from characteristics.
  3. **Scaling adjustments** — empirical calibrations for known biases; IPOs receive uplift factors.

### Universe effects on model behavior (§5.5)
Universe choice materially changes interpretation:
- Factor returns are **relative to the universe** (large-cap value ≠ all-cap value).
- Standardization parameters shift — the same company has different z-scores across universes.
- Portfolio measurement is universe-dependent (small-cap managers need small-cap-estimated models).

### Estimation-universe span and extrapolation (§5.6)
The estimation universe defines a **region in the factor coordinate system**. Portfolios lying outside that region have unreliable, **extrapolated** exposures. An **out-of-universe flag** marks both exposure unreliability and model-of-model specific-risk forecasts.

### Linked assets (§5.7)
ADRs/local shares, dual listings, and share classes represent the same company and violate the uncorrelated-residuals assumption. Treatment: designate a **primary line**, inherit its factor exposures (currency-adjusted in global models), and model linked-line specific returns as the primary's plus a small line-specific spread — a **shared specific-risk block**, not independent diagonal entries.

### Regional vs global models (§5.8)
Multi-country models partition a global estimation universe by country, adding **country membership and currency factors** alongside global industry and style factors (e.g., a French bank loads on world market, country, industry, and currency factors simultaneously). Global and regional models can disagree — they estimate factor returns on different reference populations and standardize against different means; both are valid in their own coordinate systems.

### Takeaway (§5.10)
Universe choice is *"a modeling decision with visible consequences: it defines what each factor return means and which segment drives it."* Survivorship and look-ahead discipline are as critical as data quality.

Section anchors: 5.1 why the two universes differ, 5.2 designing an estimation universe, 5.3 weighting, 5.4 extending to coverage, 5.5 universe effects, 5.6 span and extrapolation, 5.7 linked assets, 5.8 regional vs global, 5.9 the mini example's universes, 5.10 summary.

## Key terms
estimation universe, coverage universe, cross-sectional regression, factor covariance matrix, liquidity, free float, market cap threshold, minimum trading history, primary listing, representativeness, identifiability, stability buffer, entry/exit thresholds, churn, point-in-time membership, survivorship bias, look-ahead bias, square root of market cap weighting, standardization, z-score, out-of-range z-scores, imputation ladder, peer-bucket mean, regression imputation, specific risk, structural model, IPO uplift, universe effects, span, extrapolation, out-of-universe flag, linked assets, ADR, dual listing, share classes, primary line, shared specific-risk block, regional model, global model, country factor, currency factor, MiniModel

## Links
- [It's Just Beta (home)](https://itsjustbeta.com/)
- [About](https://www.itsjustbeta.com/about/)
- [01 Why Factor Models Exist](https://www.itsjustbeta.com/chapters/01-introduction/)
- [02 The Factor Model Equation](https://www.itsjustbeta.com/chapters/02-the-factor-model-equation/)
- [03 Factors and Exposures](https://www.itsjustbeta.com/chapters/03-factors-and-exposures/)
- [04 Types of Factor Model](https://www.itsjustbeta.com/chapters/04-model-types/)
- [05 Estimation Universe and Coverage Universe (this page)](https://www.itsjustbeta.com/chapters/05-universes/)
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
- [← Part 04 Types of Factor Model (prev)](https://www.itsjustbeta.com/chapters/04-model-types/)
- [Part 06 Estimating Factor Returns: The Cross-Sectional Regression → (next)](https://www.itsjustbeta.com/chapters/06-estimating-factor-returns/)
- [Feedback contact: chris (at) itsjustbeta.com](mailto:chris@itsjustbeta.com)
