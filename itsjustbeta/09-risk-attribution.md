# Risk Attribution: Where Does My Risk Come From?
URL: https://www.itsjustbeta.com/chapters/09-risk-attribution/

## Summary

Chapter 09 (Part 09, "Applications" section) is the risk-attribution chapter: given an assembled
factor risk model, decompose portfolio risk to answer three questions — how much risk exists, what
sources generate it, and which positions create it. The decomposition is "pure algebra" on the
assembled model.

### The two-level decomposition (factor vs specific)

The foundational equation:

```
σ² = x⊤Fx + w⊤Δw,   with x = X⊤w
```

- `σ²` = total portfolio variance
- `x⊤Fx` = factor risk (factor exposures `x` through the factor covariance matrix `F`)
- `w⊤Δw` = specific risk (weights through the diagonal specific-variance matrix `Δ`)
- Factor risk and specific risk are uncorrelated and sum exactly — the first split is unambiguous.

The second level (per-factor split of factor variance) hits **the cross-term problem**: factor
variance contains pairwise covariances, so per-factor attribution is ambiguous. The **standard
convention** allocates to factor k the **Contribution to Variance (CTV)**:

```
CTVₖ = xₖ(Fx)ₖ = xₖ ∑ₗ Fₖₗ xₗ
```

i.e. exposure times that factor's covariance with the whole portfolio. Cross-terms split 50/50
between factor pairs, and contributions can legitimately be **negative** (hedging positions).

### Position-level analysis: MCR and CTR

**Marginal Contribution to Risk (MCR)** — the derivative of portfolio volatility w.r.t. weight:

```
MCRᵢ = ∂σ/∂wᵢ = (Σw)ᵢ / σ
```

A *rate* — risk per unit weight, the slope of volatility along position i. It depends on the entire
portfolio context (covariances with everything else), not on the stock alone.

**Contribution to Risk (CTR)** via the **Euler decomposition** (volatility is homogeneous of
degree 1 in weights):

```
σ = ∑ᵢ wᵢ ∂σ/∂wᵢ = ∑ᵢ CTRᵢ,   CTRᵢ = wᵢ · MCRᵢ
```

CTR is the only marginal-based position decomposition without cross-term allocation; it sums
exactly to total volatility and is the standard position-level report.

**Distinction table:**
- **MCR** — a rate/sensitivity; nothing sums.
- **CTR** — position share (weight × MCR); sums to total risk (volatility units).
- **CTV** — factor share (exposure × covariance-with-portfolio); sums to factor **variance**.
- CTR (volatility) and CTV (variance) are different unit pairings — cannot compare directly.

### Active risk / tracking error

All decompositions apply unchanged to **active weights** `wₐ = wₚ − w_b` (portfolio minus
benchmark), with **tracking error (TE)** replacing volatility. The benchmark-relative view reveals
"intentional bets and accidents" — actual decisions become visible once the market-dominated total
risk is netted out.

### Mini example findings

Portfolio vs cap-weighted benchmark:
- Total vol / TE: 17.55% / 5.42%
- Active factor exposures: +0.385 VALUE (intended), −0.332 MOM (unintended shadow exposure), −0.145 TECH
- Active variance breakdown: 18% industries, 42.2% styles (of which 22% from accidental MOM), 39.8% specific
- Position concentration: the AXIOM short drives 61.7% of TE

Three recurring patterns: "Unintended style bets ride on intended ones," "tracking error
concentrates in few names," and "diversified holdings share one factor profile."

### Stress testing & scenario analysis

A scenario is a factor shock vector `fˢʰᵒᶜᵏ`; portfolio impact is `x⊤fˢʰᵒᶜᵏ`.

**Correlated (conditional) shock** — propagate a single-factor shock through the factor covariance
matrix rather than shocking one factor in isolation:

```
E[f | fₖ = s] = F₍,ₖ₎ / Fₖₖ · s
```

The conditioning reads comovements from the risk model's factor covariance `F`. Mini example: a
VALUE −8% shock implies MKT +6.4% and MOM +5.4%, giving an active impact of −6.1% versus the naive
single-factor −3.1%. **Historical scenario replay** pushes realized factor returns from crisis
periods through current exposures, surfacing correlation breakdowns.

### Takeaways

1. Risk decomposition is pure algebra on the assembled model.
2. The active/TE view reveals decisions; total risk is dominated by benchmark-related factors.
3. CTR (position, volatility) and CTV (factor, variance) are different unit pairings.
4. Stress tests must use correlated shocks, not isolated single-factor moves.
5. Attribution surfaces unintended exposures and TE concentration.

## Key terms

risk attribution, factor risk, specific risk, cross-term problem, contribution to variance, CTV, marginal contribution to risk, MCR, contribution to risk, CTR, Euler decomposition, active weights, active risk, tracking error, TE, factor covariance matrix, specific variance, unintended style bets, shadow exposure, stress testing, scenario analysis, factor shock vector, correlated shock, conditional shock, historical scenario replay, correlation breakdown, benchmark-relative, hedging positions, negative contribution

## Links

- [About](https://www.itsjustbeta.com/about/)
- [Home](https://www.itsjustbeta.com/)
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
- [← Part 08 Risk Model Assembly](https://www.itsjustbeta.com/chapters/08-risk-model-assembly/) (prev)
- [Part 10 Performance Attribution →](https://www.itsjustbeta.com/chapters/10-performance-attribution/) (next)
- Feedback email: chris [at] itsjustbeta.com (address obfuscated on page)
