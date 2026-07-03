# Review packet for Chris — decisions and findings, 2026-07-03

DRAFT — Conor to review before sending. Everything below is live in the tool; numbers are from
the 2024-12-31 book unless dated otherwise.

## 1. What changed since your last review

We aligned the tool to the primer and to your process end to end. New since Step 15: Euler
contributions (CTV/CTR) as first-class cube measures; a Model lens (rolling bias calibration,
regression fit health, factor covariance, exposure profiles, pure factor portfolios);
conditional and correlation-stressed shocks; reconcile driver reads at factor and stock level
(exposure/weight migration vs factor move vs idiosyncratic, hidden-beta flags, residual
co-movement); risk-manager commentary in one consistent voice across every panel. Every
cube-served number carries a live numpy cross-check; the tie-outs read at machine epsilon
(~1e-17).

## 2. Decisions to ratify

**2a. Model vol is now the reference risk number; your limit was rewritten.**
We took the primer at its word: σ = √(x'Fx + w'Δw) with the factor/specific split leads every
display (book: 1.44%/d, 94% factor / 6% specific). The limits are written on the
Kupiec-backtested metrics — **your Total VaR 99 limit (4.5%/5.5%) is now a Scenario VaR 99
limit at the same thresholds**; ES 97.5 unchanged (5.0%/6.5%). The old Total VaR 99 composite
(empirical factor quantile ⊕ Gaussian specific tail) survives as a measure marked "legacy".
Confirm or amend.

**2b. Views removed to match your process.**
Cut: the standalone per-bucket Scenario-VaR "Risk by level" tab (superseded by CTR — ch 09's
point that standalone bucket risk doesn't sum), the drawdown panel, the liquidity
days-to-liquidate lens, and the Basel traffic light (Kupiec + exception rate remain). Replaced:
Risk HHI → **Top-5 risk share** (currently 29.5% vs 40%/50% limits — the ch-09 CTR
concentration idiom). All endpoints are parked, not deleted; anything cut in error is a
one-line restore.

## 3. One architecture go/no-go: cube scenario branches

What-if trades and custom stresses are now priced twice: the API engine, and a transient cube
scenario branch — they agree at ~1e-17, including the drop-the-top-name demo. Promoting the
branch past prototype buys: any measure, any slice, any drill **under a hypothetical trade**
(the "measures live in the cube" principle taken to its end). Costs: scenario plumbing in the
pivot API, and a lifecycle/concurrency policy for named scenarios. Attribution measures stay
deliberately branch-insensitive (a what-if shouldn't rewrite realized history). Go/no-go?

## 4. Standing questions from the attribution build (still open from 2026-06-30)

1. Default reporting period — trailing-12m + since-inception, or a fixed quarterly grid too?
2. Hero chart grouping — Market / Style / Specific, or break out top style factors?
3. RAG thresholds for the residual diagnostics — we started loose (IR ±0.3, vol-ratio
   0.8–1.25, |autocorr| 0.2/0.35, resid-R² 0.10/0.25); tighten now or after more history?
4. Dividends — still price-only both sides. Fine for the POC, or do you need a total-return
   headline (needs a paid dividend source)?

## 5. Model findings that imply model changes (your call before we touch the builder)

**5a. Growth fails the admission bar.** |t| > 2 on only **9% of days** (your ≥⅓ rule; next
worst: NonLinSize 18%, EarnYield 21%). A noise factor leaks real risk into wrong buckets —
propose dropping it (builder change + rebuild).

**5b. The specific block under-forecasts.** Rolling bias b = **1.26** (outside the 1±0.29
band; window peak 4.8) with **16.7% two-sigma exceedances vs 4.6% expected**. The book-level
forecast is conservative (b = 0.55) so this nets out today, but the specific number itself
shouldn't be trusted in stress months. Candidates: shorten the EWMA half-life, or add the
structural (characteristics-based) blend from ch 08. Some of it is 13F staleness landing in
"specific" — irreducible on quarterly filings.

**5c. Two suspect Liquidity loadings.** GFL and SNY breached their reconcile bands
factor-driven while the Liquidity factor itself stayed within band — the hidden-beta signature
(realized comovement exceeds the modeled loading). GFL is also the book's worst
days-to-liquidate name. Propose: inspect their Liquidity descriptors (exposure-profile view)
and check the trailing-63d dollar-ADV inputs before trusting their marginals.

**5d. Informational, no action:** NonLinSize is running ~1.2× its full-history vol (the one
vol-clustering flag) — full-history bands understate it; the Q4 reconcile breach on it was
genuine and correctly flagged.

## 6. The benchmark (the one thing we need from you to unlock active space)

Everything is built absolute-first; a benchmark turns on tracking error, active attribution,
Brinson, and min-TE construction in one move. On free data the practical candidates are S&P 500
price-return (matches our price-only convention) via the index constituents we already carry.
Name your preferred benchmark (and whether price-only is acceptable for it) and Step 13
unblocks.

---
*Also for your radar: chapters 11–15 of the primer are still "coming soon" — when hedging and
model-evaluation publish we'll re-audit our `/hedge` and `/calibration` designs against the
text.*
