// Model vs Price lens (docs/price-var-plan.md). The MODEL prices the portfolio on a linear
// factor block + a Gaussian diagonal specific block (Total VaR 99); PRICE prices the SAME portfolio on raw
// historical stock returns — no model at all (Price VaR 99). /var_bridge explains the gap in
// four ordered, additive terms. Tufte/Few: grey + one accent, direct labels, no legend.
import { useApp } from "../context/AppContext";
import { useVarBridge } from "../api/hooks";
import { LineChart } from "../components/LineChart";
import { StreamPanel } from "../components/StreamPanel";
import { QueryState, HowToRead } from "../components/ui";
import { pct, num } from "../lib/format";
import type { VarBridgeStep, VarBridgeTerms, VarBridgeDisagreement } from "../api/types";

const FAINT = "#6b6b63", ACCENT = "#3b5e8c", INK = "#111", AMBER = "#b07d2b";

const TERM_LABEL: Record<keyof VarBridgeTerms, string> = {
  specific_risk_dropped: "specific risk dropped",
  specific_distribution: "specific distribution",
  exposure_drift: "exposure drift",
  coverage: "coverage",
};

function driverColor(d: string): string {
  if (d.startsWith("coverage")) return AMBER;
  if (d === "specific_risk_dropped") return ACCENT;
  return "#9a968c";
}

// ---- the five-bar waterfall: T0..T4 levels (ink/grey) + four labelled delta bars (accent) ----
function Waterfall({ steps, terms }: { steps: VarBridgeStep[]; terms: VarBridgeTerms }) {
  const W = 720, H = 210, pad = { l: 12, r: 10, t: 16, b: 44 };
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
  const levels = steps.map((s) => s.value);
  const vMax = Math.max(...levels) * 1.15 || 1;
  const n = 9;                                   // 5 levels + 4 deltas, interleaved
  const slot = iw / n;
  const barW = slot * 0.64;
  const sy = (v: number) => pad.t + (1 - v / vMax) * ih;
  const levelX = (i: number) => pad.l + 2 * i * slot + (slot - barW) / 2;
  const deltaX = (i: number) => pad.l + (2 * i + 1) * slot + (slot - barW) / 2;
  const termKeys = Object.keys(terms) as (keyof VarBridgeTerms)[];
  return (
    <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ maxWidth: W }} role="img"
      aria-label="Model-vs-Price bridge waterfall">
      <line x1={pad.l} x2={pad.l + iw} y1={pad.t + ih} y2={pad.t + ih} stroke="#c9c5bb" strokeWidth={1} />
      {steps.map((s, i) => {
        const x = levelX(i), y = sy(s.value);
        const end = i === 0 || i === steps.length - 1;
        return (
          <g key={s.step}>
            <rect x={x} y={y} width={barW} height={Math.max(0, pad.t + ih - y)}
              fill={end ? INK : "#9a968c"} />
            <text x={x + barW / 2} y={y - 5} textAnchor="middle" fontSize={11.5} className="num" fill={INK}>
              {pct(s.value, 2)}
            </text>
            <text x={x + barW / 2} y={pad.t + ih + 14} textAnchor="middle" fontSize={10.5} fill={FAINT}>
              {s.step}
            </text>
          </g>
        );
      })}
      {termKeys.map((k, i) => {
        const from = levels[i], to = levels[i + 1];
        const v = terms[k];
        const x = deltaX(i);
        const yTop = sy(Math.max(from, to)), yBot = sy(Math.min(from, to));
        return (
          <g key={k}>
            <rect x={x} y={yTop} width={barW} height={Math.max(1, yBot - yTop)} fill={ACCENT} opacity={0.8} />
            <text x={x + barW / 2} y={yTop - 5} textAnchor="middle" fontSize={10} className="num" fill={INK}>
              {v >= 0 ? "+" : "−"}{pct(Math.abs(v), 2)}
            </text>
            <text x={x + barW / 2} y={pad.t + ih + 14} textAnchor="middle" fontSize={9} fill={FAINT}>
              {TERM_LABEL[k].split(" ")[0]}
            </text>
            <text x={x + barW / 2} y={pad.t + ih + 24} textAnchor="middle" fontSize={9} fill={FAINT}>
              {TERM_LABEL[k].split(" ").slice(1).join(" ")}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

// ---- per-name scatter: Marginal Total VaR (model, x) vs Marginal Price VaR (price, y) ----
function DisagreementScatter({ rows }: { rows: VarBridgeDisagreement[] }) {
  const W = 380, H = 380, pad = { l: 12, r: 14, t: 10, b: 34 };
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
  if (!rows.length) return <div className="muted small">no positions</div>;
  const xs = rows.map((r) => r["Marginal Total VaR 99"]);
  const ys = rows.map((r) => r["Marginal Price VaR 99"]);
  const vMax = Math.max(...xs, ...ys, 0) * 1.15 || 1;
  const vMin = Math.min(...xs, ...ys, 0);
  const sx = (v: number) => pad.l + ((v - vMin) / (vMax - vMin)) * iw;
  const sy = (v: number) => pad.t + (1 - (v - vMin) / (vMax - vMin)) * ih;
  const wMax = Math.max(...rows.map((r) => Math.abs(r.weight)), 1e-6);
  return (
    <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ maxWidth: W }} role="img"
      aria-label="Marginal Total VaR vs Marginal Price VaR by name">
      <line x1={sx(vMin)} y1={sy(vMin)} x2={sx(vMax)} y2={sy(vMax)} stroke="#c9c5bb" strokeDasharray="2 2" />
      {rows.map((r) => (
        <circle key={r.position} cx={sx(r["Marginal Total VaR 99"])} cy={sy(r["Marginal Price VaR 99"])}
          r={2.5 + Math.sqrt(Math.abs(r.weight) / wMax) * 9}
          fill={driverColor(r.likely_driver)} opacity={0.72}>
          <title>{`${r.ticker.toUpperCase()} — model ${pct(r["Marginal Total VaR 99"], 2)}, `
            + `price ${pct(r["Marginal Price VaR 99"], 2)} (${r.likely_driver})`}</title>
        </circle>
      ))}
      <text x={pad.l} y={H - 4} fontSize={10} fill={FAINT}>Marginal Total VaR 99 (model) →</text>
      <text x={-H + 4} y={12} fontSize={10} fill={FAINT} transform="rotate(-90)">
        Marginal Price VaR 99 (price) →
      </text>
    </svg>
  );
}

export function ModelVsPrice() {
  const { date, manager, scenario } = useApp();
  const q = useVarBridge(date, manager, scenario);

  return (
    <main className="lens">
      <h1>Model vs Price</h1>
      <p className="sub">
        {manager} · as-of {date} · set {scenario} — the model&rsquo;s factor + Gaussian-specific
        VaR against a historical simulation on raw stock returns, no model at all
      </p>

      <QueryState q={q}>
        {(b) => {
          const t1 = b.steps.find((s) => s.step === "T1")!.value;
          const t4 = b.steps.find((s) => s.step === "T4")!.value;
          const largest = (Object.entries(b.terms) as [keyof VarBridgeTerms, number][])
            .sort((a, c) => Math.abs(c[1]) - Math.abs(a[1]))[0];
          return (
            <>
              <div className="hgroup">
                <div className="hero">
                  <div className="k">Total VaR 99 (model)</div>
                  <span className="v">{pct(t1, 2)}</span>
                </div>
                <div className="hero">
                  <div className="k">Price VaR 99 (raw tape)</div>
                  <span className="v">{pct(t4, 2)}</span>
                </div>
                <div className="hero">
                  <div className="k">Largest bridge term</div>
                  <span className="v" style={{ fontSize: 20 }}>{TERM_LABEL[largest[0]]}</span>
                  <div className="k">{largest[1] >= 0 ? "+" : "−"}{pct(Math.abs(largest[1]), 2)}</div>
                </div>
                <div className="hero">
                  <div className="k">Coverage at the price tail</div>
                  <span className="v">{pct(b.coverage.at_tail, 0)}</span>
                </div>
              </div>

              <h2>The bridge</h2>
              <Waterfall steps={b.steps} terms={b.terms} />
              <table className="tufte" style={{ marginTop: "0.6rem", maxWidth: "40rem" }}>
                <thead><tr><th className="label">Step</th><th>Value</th><th className="label">What changes</th></tr></thead>
                <tbody>
                  {b.steps.map((s) => (
                    <tr key={s.step}>
                      <td className="label">{s.step} — {s.measure}</td>
                      <td>{pct(s.value, 2)}</td>
                      <td className="label muted small">{s.what_changes}</td>
                    </tr>
                  ))}
                </tbody>
              </table>

              <h2>Coverage</h2>
              <p className="small">
                {pct(b.coverage.at_tail, 1)} of portfolio weight was actually priced on the portfolio&rsquo;s own
                Price-VaR tail day.{" "}
                {b.coverage.never_priced_weight > 0 &&
                  `${pct(b.coverage.never_priced_weight, 1)} of weight has NO price history at all `
                  + `(${b.coverage.never_priced_names.slice(0, 5).map((n) => n.ticker.toUpperCase()).join(", ")}).`}
                {" "}
                {b.coverage.added_by_coverage_weight > 0 &&
                  `${pct(b.coverage.added_by_coverage_weight, 1)} of weight is priced but has no factor `
                  + `loading (${b.coverage.added_by_coverage_names.slice(0, 5).map((n) => n.ticker.toUpperCase()).join(", ")}).`}
              </p>
              <LineChart title="Coverage by day" width={700} height={110}
                points={b.coverage.series.dates.map((_, i) => ({ x: i, y: b.coverage.series.coverage[i] }))}
                labels={b.coverage.series.dates} fmt={(v) => pct(v, 0)} />

              <h2>Per-name disagreement — Marginal Total VaR vs Marginal Price VaR</h2>
              <div className="row" style={{ gap: "1.6rem", alignItems: "flex-start", flexWrap: "wrap" }}>
                <DisagreementScatter rows={b.disagreements} />
                <table className="tufte" style={{ minWidth: "22rem" }}>
                  <thead>
                    <tr><th className="label">Name</th><th>Model</th><th>Price</th><th>Gap</th>
                      <th className="label">Likely driver</th></tr>
                  </thead>
                  <tbody>
                    {b.disagreements.slice(0, 12).map((r) => (
                      <tr key={r.position}>
                        <td className="label">{r.ticker.toUpperCase()}</td>
                        <td>{pct(r["Marginal Total VaR 99"], 2)}</td>
                        <td>{pct(r["Marginal Price VaR 99"], 2)}</td>
                        <td>{r.gap >= 0 ? "+" : "−"}{pct(Math.abs(r.gap), 2)}</td>
                        <td className="label small muted">{r.likely_driver}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="muted small">
                verification: numpy Price VaR 99 diff {num(b.verification.diff, 6)}; coverage-at-tail
                diff {num(b.verification.coverage_diff, 6)}.
              </p>

              <HowToRead>
                <p>
                  Five numbers in a fixed order, each changing ONE thing from the previous
                  (docs/price-var-plan.md): T0 Scenario VaR 99 (factor-only) &rarr; T1 Total VaR 99
                  (+ Gaussian specific block) &rarr; T2 full-sim VaR (the Gaussian block replaced by
                  REALIZED daily residual paths) &rarr; T3 Price VaR on covered names (today&rsquo;s
                  loadings replaced by each day&rsquo;s own — the model&rsquo;s own identity
                  r<sub>t</sub> = L<sub>i</sub>(t)&middot;f<sub>t</sub> + u<sub>i,t</sub>) &rarr; T4 Price
                  VaR 99 (names priced but uncovered by the model are added). The four differences sum
                  to T4&minus;T0 exactly.
                </p>
                <p>
                  A large <strong>specific distribution</strong> term is the missing-factor signal:
                  realized residual correlation the Gaussian diagonal block cannot represent (see
                  Attribution &rarr; PnL for the residual diagnostics). A large{" "}
                  <strong>exposure drift</strong> term means the portfolio&rsquo;s loadings moved a lot over
                  the window — check Drift for rotation (deliberate) vs re-pricing (unintentional).
                  A large <strong>coverage</strong> term is a model blind spot, not a risk number to act
                  on directly. The per-name driver label is a heuristic on magnitude, not an exact
                  per-name split — &ldquo;coverage&rdquo; is the only exact one.
                </p>
              </HowToRead>

              <hr className="rule" />
              <div style={{ maxWidth: "46rem" }}>
                <h2>Risk-manager read</h2>
                <StreamPanel path="/var_bridge/analysis"
                  body={{ date, manager, set: scenario }}
                  cacheKey={`var_bridge:${date}:${manager}:${scenario}`}
                  label="Generate Model-vs-Price read" />
              </div>
            </>
          );
        }}
      </QueryState>
    </main>
  );
}
