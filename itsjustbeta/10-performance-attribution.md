# Performance Attribution: Where Did My Returns Come From?
URL: https://www.itsjustbeta.com/chapters/10-performance-attribution/

## Summary

Chapter 10 (Part 10, "Applications" section) decomposes *realized* returns through the factor
model: split active return into per-factor contributions plus a specific (stock-selection) part,
link contributions across periods so they reconcile with the geometric cumulative return, and read
the result as tilt vs skill. This is the chapter to compare against a real attribution
implementation.

### §10.1 Single-period decomposition (exact)

The active return decomposition — exact, no cross-terms, because returns are linear:

```
r_a = w_a⊤ r = w_a⊤ (X f + ϵ) = x_a⊤ f + w_a⊤ ϵ = ∑ₖ x_{a,k} f_k + specific
```

Notation:
- `r_a` — active return (portfolio minus benchmark)
- `w_a` — active weights (portfolio minus benchmark), N×1
- `r` — asset returns over one period, N×1
- `X` — factor exposure matrix, N×K
- `f` — factor returns over the period, K×1
- `ϵ` — specific/idiosyncratic returns, N×1
- `x_a = X⊤w_a` — active factor exposures, K×1

Each factor's contribution is `x_{a,k} · f_k`: "the return on the x_{a,k} units of the pure
factor-k portfolio the manager implicitly held." Unlike risk attribution (Ch. 09), there is **no
cross-term problem** — returns are linear, so single-period attribution is exact.

**Mini example, Month 1** — active return −0.641%:

| Source | Exposure × Return | Contribution |
|--------|------------------|--------------|
| MKT | 0.00 × 1.82% | 0.000% |
| TECH | −0.145 × 0.77% | −0.112% |
| FIN | +0.095 × (−1.28%) | −0.122% |
| VALUE | +0.385 × 0.55% | +0.211% |
| MOM | −0.332 × 1.96% | −0.651% |
| Factor total | | −0.671% |
| Specific | | +0.030% |
| Active return | | −0.641% |

The −0.332 momentum exposure was unintended (a shadow of the value tilt) but dominated the month —
costly during a strong momentum month.

### §10.2 Multi-period treatment: the compounding problem and linking algorithms

**The compounding problem:** single-period arithmetic contributions do not sum to the multi-period
geometric (compounded) return. Mini example: three months of period-by-period arithmetic summation
gave −0.146%, but the true compounded active return was −0.102% — a 4.4bp discrepancy that grows
with horizon and volatility.

**Linking algorithms** reconcile arithmetic contributions with the geometric total:

1. **Cariño linking** — rescale each period's contributions by `κ_t / κ`, where the per-period
   log-linearization coefficient is

   ```
   κ_t = [ln(1 + r_{p,t}) − ln(1 + r_{b,t})] / (r_{p,t} − r_{b,t})
   ```

   and `κ` is the same expression applied to the *cumulative* portfolio and benchmark returns.
   Intuition: convert to log-return space (which is time-additive), distribute contributions
   there, convert back. Result: "Contributions then sum exactly to the true cumulative active
   return."

2. **Menchero (optimized linking)** — choose uniform per-period scaling factors that minimize
   distortion while achieving exact reconciliation.

3. **Frongello / GRAP** — order-dependent sequential compounding: scale early periods by later
   benchmark growth and vice versa.

The chapter's guidance: "All produce the same totals and qualitatively similar splits. Consistency
matters more than which one you pick."

**Cariño-linked quarter results (mini example):** VALUE +0.46%, MOM −0.72%, specific +0.24%,
total −0.102% — exactly matching the compounded portfolio-minus-benchmark differential.

### §10.3 Skill vs tilt interpretation

The factor/specific split is the industry's tilt-vs-skill measure:

- **Factor-tilt managers** — returns come from stable factor exposures; valuable but replicable
  through cheap factor products, and fees should reflect that.
- **Stock-selection managers** — returns dominated by the specific contribution; true idiosyncratic
  skill the model cannot replicate.

**Caveats on interpreting specific returns:**

- *Statistical:* "A +24bp specific quarter is noise." Significance follows `t = IR√T`
  (information ratio times √periods): an IR of 0.5 needs `T = (2/0.5)²` ≈ 16 years to reach two
  standard errors; an IR of 1.0 needs ~4 years.
- *Structural:* specific returns are "what this model's factors don't span" — sharper models
  reclassify yesterday's alpha as today's beta (example given: a crowding factor recategorizing
  stat-arb returns).

Mini-example diagnosis: the problem was the **momentum leak** (unintended exposure), not stock
selection — the fix is hedging/neutralization in portfolio construction (Ch. 11), not a process
change.

### §10.4 Factor-based vs Brinson attribution

**Brinson** needs no risk model. It splits active return by one grouping (sector/country):

- **Allocation** (overweighting winning sectors):

  ```
  ∑ⱼ (w_{p,j} − w_{b,j}) (r_{b,j} − r_b)
  ```

- **Selection** (picking winners within sectors):

  ```
  ∑ⱼ w_{p,j} (r_{p,j} − r_{b,j})
  ```

- The **Brinson-Fachler** variant uses `w_{b,j}` in selection and reports an **interaction** term
  separately.

Comparison:

| Aspect | Factor-based | Brinson |
|--------|--------------|---------|
| Dimensions | All factors simultaneously | One grouping (sector/country) |
| Style effects | Explicit lines | Invisible (buried in "selection") |
| Model dependence | Full risk model | None |
| Audience | Quant / style-aware | Classic sector rotation, client reporting |

Key difference: Brinson's within-sector "selection" conflates all style effects — a value manager
shows brilliant/terrible "selection" when value works/fails, regardless of actual stock-picking.
Factor attribution exists to break this conflation. **Best practice: run both reports**, reconcile
them, and explain the differences — the differences themselves illuminate manager intent.

### §10.5 Pitfalls

1. **Model misspecification leakage** — a missing factor lands its returns in the residuals:
   "A missing factor lands its returns in the residuals, so attribution flatters or damns stock
   selection erroneously." A large specific line *correlated with the portfolio* signals a missing
   factor, not skill.
2. **Horizon/model mismatch** — "Attributing a daily-turnover book with a monthly model … misstates
   exposures during the period." Frequencies must align; Chapter 14 covers fit-for-purpose testing.
3. **Exposure timing & trading residual** — "Real portfolios trade intra-period. Using
   start-of-period holdings creates a trading residual (transaction costs plus intra-period
   exposure drift) that should be reported as its own line," not lumped into specific. Production
   attribution recomputes daily and links.
4. **Model dependency** — the VALUE contribution depends on this model's value definition
   (descriptors, universe). Attribution outputs are "model-conditional statements" — always quote
   the model with the numbers.

### §10.6 Summary

- Single-period is exact: `r_a = ∑ₖ x_{a,k} f_k + w_a⊤ϵ`; contributions are returns on implicit
  pure factor portfolios.
- Multi-period contributions need a linking algorithm (Cariño, Menchero, Frongello/GRAP) to
  reconcile arithmetic with geometric; once linked they sum exactly to the cumulative active return.
- The factor/specific split is the tilt-vs-skill measure, with model-dependence as the standing
  caveat.
- Mini example: VALUE +46bp, MOM −72bp, specific +24bp = −10bp net; fix the unintended exposure via
  portfolio construction (Chapter 11).

## Key terms

performance attribution, return decomposition, active return, active weights, active factor exposures, factor contribution, specific return, idiosyncratic return, residual, pure factor portfolio, compounding problem, arithmetic vs geometric, multi-period linking, linking algorithm, Carino linking, Cariño, kappa, log-return space, Menchero, optimized linking, Frongello, GRAP, cumulative active return, skill vs tilt, factor tilt manager, stock selection, information ratio, IR, t-stat, t = IR√T, time to significance, crowding factor, Brinson, Brinson-Fachler, allocation effect, selection effect, interaction term, sector rotation, model misspecification, missing factor, residual leakage, horizon mismatch, exposure timing, trading residual, transaction costs, exposure drift, start-of-period holdings, daily recomputation, model-conditional, momentum leak, unintended exposure, hedging, neutralization

## Links

- [Home / Chapters](https://www.itsjustbeta.com/)
- [About](https://www.itsjustbeta.com/about/)
- [01 Why Factor Models Exist](https://www.itsjustbeta.com/chapters/01-introduction/)
- [02 The Factor Model Equation](https://www.itsjustbeta.com/chapters/02-the-factor-model-equation/)
- [03 Factors and Exposures](https://www.itsjustbeta.com/chapters/03-factors-and-exposures/)
- [04 Types of Factor Model](https://www.itsjustbeta.com/chapters/04-model-types/)
- [05 Estimation Universe and Coverage Universe](https://www.itsjustbeta.com/chapters/05-universes/)
- [06 Estimating Factor Returns: The Cross-Sectional Regression](https://www.itsjustbeta.com/chapters/06-estimating-factor-returns/)
- [07 Factor Portfolios](https://www.itsjustbeta.com/chapters/07-factor-portfolios/)
- [08 Risk Model Assembly](https://www.itsjustbeta.com/chapters/08-risk-model-assembly/)
- [09 Risk Attribution](https://www.itsjustbeta.com/chapters/09-risk-attribution/)
- [10 Performance Attribution](https://www.itsjustbeta.com/chapters/10-performance-attribution/) (current page)
- [11 Portfolio Construction](https://www.itsjustbeta.com/chapters/11-portfolio-construction/)
- [12 Hedging](https://www.itsjustbeta.com/chapters/12-hedging/)
- [13 Alpha Research](https://www.itsjustbeta.com/chapters/13-alpha-research/)
- [14 Evaluating a Factor Model](https://www.itsjustbeta.com/chapters/14-model-evaluation/)
- [15 Modifying a Factor Model](https://www.itsjustbeta.com/chapters/15-modifying-the-model/)
- [16 Practical Considerations](https://www.itsjustbeta.com/chapters/16-practical-considerations/)
- [17 Appendix: Reference Material](https://www.itsjustbeta.com/chapters/17-appendix/)
- [18 Mini Example Source Code](https://www.itsjustbeta.com/chapters/18-mini-example-source-code/)
- [← Part 09 Risk Attribution](https://www.itsjustbeta.com/chapters/09-risk-attribution/) (prev)
- [Part 11 Portfolio Construction →](https://www.itsjustbeta.com/chapters/11-portfolio-construction/) (next)
- Feedback email: chris [at] itsjustbeta.com (address obfuscated on page)
