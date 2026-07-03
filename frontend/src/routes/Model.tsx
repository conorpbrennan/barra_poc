// Model lens — the fit-for-purpose family (It's Just Beta ch 06 + 08):
//   * Calibration: rolling bias statistic b vs the 1 ± √(2/W) acceptance band (book + specific),
//     plus 2σ exceedance counts — where in the calendar the risk forecast drifted.
//   * Regression health: monthly weighted cross-sectional R² trend and the per-factor |t|>2
//     admission table from the builder's regression_stats artifact.
//   * Factor covariance: the F matrix made visible — shaded correlations, full vs recent-1y vols.
// Tufte/Few: small multiples on shared scales, direct labels, shading only where it encodes data.
import { useState } from "react";
import { useApp } from "../context/AppContext";
import {
  useCalibration, useRegression, useFactorCov, useExposureProfile, useFactorPortfolio, useMeta,
} from "../api/hooks";
import { QueryState } from "../components/ui";
import { pct, num, signedNum } from "../lib/format";
import type { ValidationSeries } from "../api/types";

const FAINT = "#6b6b63", ACCENT = "#3b5e8c", INK = "#111";

// ---- rolling-bias line with the acceptance band (exported for tests) ----
export function BiasChart({ s, label, width = 340, height = 150 }: {
  s: ValidationSeries; label: string; width?: number; height?: number;
}) {
  const pad = { l: 30, r: 8, t: 10, b: 16 };
  const iw = width - pad.l - pad.r, ih = height - pad.t - pad.b;
  const n = s.bias.length;
  if (n < 2) return <div className="muted small">insufficient history</div>;
  const ys = s.bias.map((p) => p.b);
  const yMax = Math.max(1 + s.band, ...ys) * 1.05;
  const yMin = Math.min(1 - s.band, ...ys, 0.5) * 0.95;
  const sx = (i: number) => pad.l + (i / (n - 1)) * iw;
  const sy = (v: number) => pad.t + (1 - (v - yMin) / (yMax - yMin)) * ih;
  const line = s.bias.map((p, i) => `${i ? "L" : "M"}${sx(i).toFixed(1)},${sy(p.b).toFixed(1)}`).join("");
  const last = s.bias[n - 1];
  const out = last.b > 1 + s.band || last.b < 1 - s.band;
  return (
    <svg width={width} height={height} role="img" aria-label={`rolling bias — ${label}`}>
      <rect x={pad.l} y={sy(1 + s.band)} width={iw}
        height={Math.max(1, sy(1 - s.band) - sy(1 + s.band))} fill="#ece9e0" />
      <line x1={pad.l} x2={pad.l + iw} y1={sy(1)} y2={sy(1)} stroke="#c9c5bb"
        strokeWidth={1} strokeDasharray="2 3" />
      <path d={line} fill="none" stroke={out ? "#a8322a" : INK} strokeWidth={1.3} />
      {[1 - s.band, 1, 1 + s.band].map((v) => (
        <text key={v} x={pad.l - 4} y={sy(v) + 3.5} textAnchor="end" fontSize={10}
          fill={FAINT} className="num">{num(v, 2)}</text>
      ))}
      <text x={pad.l} y={height - 3} fontSize={10} fill={FAINT}>{s.bias[0].date.slice(0, 7)}</text>
      <text x={pad.l + iw} y={height - 3} textAnchor="end" fontSize={10} fill={FAINT}>
        {last.date.slice(0, 7)}</text>
      <text x={pad.l + 4} y={pad.t + 10} fontSize={11} fill={INK}>{label}</text>
      <text x={pad.l + iw} y={sy(last.b) - 5} textAnchor="end" fontSize={10.5}
        fill={out ? "#a8322a" : FAINT} className="num">b {num(last.b, 2)}</text>
    </svg>
  );
}

function R2Chart({ pts, width = 700, height = 130 }: {
  pts: { date: string; r2: number }[]; width?: number; height?: number;
}) {
  const pad = { l: 34, r: 8, t: 8, b: 16 };
  const iw = width - pad.l - pad.r, ih = height - pad.t - pad.b;
  const n = pts.length;
  if (n < 2) return null;
  const yMax = Math.max(...pts.map((p) => p.r2)) * 1.1;
  const sx = (i: number) => pad.l + (i / (n - 1)) * iw;
  const sy = (v: number) => pad.t + (1 - v / yMax) * ih;
  const line = pts.map((p, i) => `${i ? "L" : "M"}${sx(i).toFixed(1)},${sy(p.r2).toFixed(1)}`).join("");
  return (
    <svg width="100%" viewBox={`0 0 ${width} ${height}`} style={{ maxWidth: width }} role="img"
      aria-label="monthly cross-sectional R²">
      {[0, 0.2, 0.4].filter((v) => v < yMax).map((v) => (
        <g key={v}>
          <text x={pad.l - 4} y={sy(v) + 3.5} textAnchor="end" fontSize={10} fill={FAINT}
            className="num">{pct(v, 0)}</text>
          <line x1={pad.l} x2={pad.l + iw} y1={sy(v)} y2={sy(v)} stroke="#ece9e0" strokeWidth={1} />
        </g>
      ))}
      <path d={line} fill="none" stroke={ACCENT} strokeWidth={1.2} />
      <text x={pad.l} y={height - 3} fontSize={10} fill={FAINT}>{pts[0].date.slice(0, 7)}</text>
      <text x={pad.l + iw} y={height - 3} textAnchor="end" fontSize={10} fill={FAINT}>
        {pts[n - 1].date.slice(0, 7)}</text>
    </svg>
  );
}

// correlation cell shading: |ρ| in grey, sign carried by the number itself
function corrBg(v: number): string {
  const a = Math.min(Math.abs(v), 1) * 0.55;
  return `rgba(107,107,99,${a.toFixed(3)})`;
}

// ---- exposure profile: one factor's cross-section, the book overlaid ----
function ExposureProfile() {
  const { date } = useApp();
  const { data: meta } = useMeta();
  const factors = meta?.factors ?? [];
  const [factor, setFactor] = useState("Size");
  const q = useExposureProfile(factor, date);
  const W = 700, H = 170, pad = { l: 10, r: 10, t: 8, b: 30 };
  return (
    <>
      <h2>Exposure profile — what the factor means here</h2>
      <div className="row" style={{ marginBottom: "0.4rem" }}>
        <select value={factor} onChange={(e) => setFactor(e.target.value)}>
          {factors.filter((f) => f !== "Market").map((f) => <option key={f}>{f}</option>)}
        </select>
      </div>
      <QueryState q={q}>
        {(p) => {
          const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
          const x0 = p.hist[0].x0, x1 = p.hist[p.hist.length - 1].x1;
          const nMax = Math.max(...p.hist.map((b) => b.n), 1);
          const sx = (v: number) => pad.l + ((v - x0) / (x1 - x0)) * iw;
          return (
            <div style={{ maxWidth: "52rem" }}>
              <p className="muted small" style={{ margin: "0 0 0.2rem" }}>
                <strong>{p.factor}</strong> = {p.recipe} · {p.n_names} names at {p.date} ·
                median {signedNum(p.quantiles.p50, 2)} ·
                beyond ±3: {p.beyond3.n} names ({pct(p.beyond3.share, 1)}
                {p.beyond3.names.length > 0 &&
                  `, e.g. ${p.beyond3.names.slice(0, 3)
                    .map((n) => `${n.ticker.toUpperCase()} ${signedNum(n.loading, 1)}`).join(", ")}`})
              </p>
              <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ maxWidth: W }} role="img"
                aria-label={`cross-section distribution of ${p.factor} loadings`}>
                {p.hist.map((b, i) => (
                  <rect key={i} x={sx(b.x0)} width={Math.max(sx(b.x1) - sx(b.x0) - 0.5, 0.5)}
                    y={pad.t + (1 - b.n / nMax) * ih} height={(b.n / nMax) * ih} fill="#c9c5bb" />
                ))}
                {[-3, 3].filter((v) => v > x0 && v < x1).map((v) => (
                  <g key={v}>
                    <line x1={sx(v)} x2={sx(v)} y1={pad.t} y2={pad.t + ih}
                      stroke="#b07d2b" strokeWidth={1} strokeDasharray="3 3" />
                    <text x={sx(v)} y={pad.t + ih + 12} textAnchor="middle" fontSize={10}
                      fill="#b07d2b">{v > 0 ? "+3 winsor" : "−3 winsor"}</text>
                  </g>
                ))}
                {p.held.map((h_) => (
                  <circle key={h_.ticker} cx={sx(Math.max(x0, Math.min(x1, h_.loading)))}
                    cy={pad.t + ih + 4} r={2 + Math.sqrt(h_.weight) * 14}
                    fill={ACCENT} opacity={0.55}>
                    <title>{`${h_.ticker.toUpperCase()} ${num(h_.loading, 2)} (w ${pct(h_.weight, 1)})`}</title>
                  </circle>
                ))}
                <text x={pad.l} y={H - 3} fontSize={10} fill={FAINT}>{num(x0, 0)}</text>
                <text x={pad.l + iw} y={H - 3} textAnchor="end" fontSize={10} fill={FAINT}>{num(x1, 0)}</text>
                <text x={pad.l + iw} y={pad.t + 10} textAnchor="end" fontSize={10.5} fill={FAINT}>
                  cross-section · dots = held book (size = weight)</text>
              </svg>
              <p className="muted small">{p.note}</p>
            </div>
          );
        }}
      </QueryState>
    </>
  );
}

// ---- factor portfolio inspector: the regression dual, f̂ = Pr ----
function FactorPortfolio() {
  const { date } = useApp();
  const { data: meta } = useMeta();
  const factors = meta?.factors ?? [];
  const [factor, setFactor] = useState("Value");
  const q = useFactorPortfolio(factor, date);
  return (
    <>
      <h2>Factor portfolio — a factor return is a portfolio return</h2>
      <div className="row" style={{ marginBottom: "0.4rem" }}>
        <select value={factor} onChange={(e) => setFactor(e.target.value)}>
          {factors.map((f) => <option key={f}>{f}</option>)}
        </select>
      </div>
      <QueryState q={q}>
        {(fp) => (
          <div style={{ maxWidth: "52rem" }}>
            <p className="muted small" style={{ margin: "0 0 0.4rem" }}>
              Pure <strong>{fp.factor}</strong> portfolio at {fp.date} · fit on {fp.n_names} names
              ({fp.fit_universe}) · gross leverage <strong>{num(fp.gross_leverage, 1)}×</strong>,
              net {signedNum(fp.net, 2)} · purity: self-exposure {num(fp.self_exposure, 3)},
              max cross {fp.max_cross_exposure.toExponential(0)}
            </p>
            <div className="row" style={{ gap: "2.5rem", alignItems: "flex-start" }}>
              {[["Long", fp.longs], ["Short", fp.shorts]].map(([label, side]) => (
                <table className="tufte" key={label as string} style={{ minWidth: "14rem" }}>
                  <thead><tr><th className="label">{label as string}</th><th>Weight</th></tr></thead>
                  <tbody>
                    {(side as { ticker: string; weight: number }[]).map((r) => (
                      <tr key={r.ticker}>
                        <td className="label">{r.ticker.toUpperCase()}</td>
                        <td>{signedPctW(r.weight)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ))}
            </div>
            <p className="muted small">{fp.note}</p>
          </div>
        )}
      </QueryState>
    </>
  );
}

function signedPctW(v: number): string {
  return `${v >= 0 ? "+" : "−"}${(Math.abs(v) * 100).toFixed(1)}%`;
}

export function Model() {
  const [window, setWindow] = useState(24);
  const vq = useCalibration(window);
  const rq = useRegression();
  const cq = useFactorCov();

  return (
    <main className="lens">
      <h1>Model</h1>
      <p className="sub">Is the model fit for purpose — calibration, fit health, the F matrix</p>

      <h2>Calibration — rolling bias statistic</h2>
      <div className="row" style={{ marginBottom: "0.5rem" }}>
        <span className="muted small">Window</span>
        <select value={window} onChange={(e) => setWindow(Number(e.target.value))}>
          {[12, 24, 36].map((w) => <option key={w} value={w}>{w}m</option>)}
        </select>
      </div>
      <QueryState q={vq}>
        {(v) => (
          <div style={{ maxWidth: "52rem" }}>
            <div className="row" style={{ gap: "1.5rem", flexWrap: "wrap" }}>
              <BiasChart s={v.series.book} label="book" />
              <BiasChart s={v.series.specific} label="specific" />
            </div>
            <p className="muted small" style={{ marginTop: "0.3rem" }}>
              b = std(realized / predicted vol) over a trailing {v.window}m window; the shaded
              band is the 95% acceptance range 1 ± √(2/{v.window}). Above the band = risk
              under-forecast. 2σ exceedances:{" "}
              book {v.series.book.exceedance_2s === null ? "—" : pct(v.series.book.exceedance_2s, 1)},
              specific {v.series.specific.exceedance_2s === null ? "—" : pct(v.series.specific.exceedance_2s, 1)}{" "}
              of months vs ≈{pct(v.expected_exceedance_2s, 1)} expected.
            </p>
          </div>
        )}
      </QueryState>

      <h2>Regression health — the cross-sectional fit</h2>
      <QueryState q={rq}>
        {(r) => (
          <div style={{ maxWidth: "52rem" }}>
            <p className="muted small" style={{ margin: "0 0 0.2rem" }}>
              Monthly-mean weighted R² of the daily WLS · overall mean {pct(r.r2_mean, 0)} ·
              estimation cross-section {r.n_names.min}–{r.n_names.max} names
              (median {num(r.n_names.median, 0)})
            </p>
            <R2Chart pts={r.r2_monthly} />
            <table className="tufte" style={{ marginTop: "0.5rem" }}>
              <thead><tr><th className="label">Factor</th><th>days |t| &gt; 2</th>
                <th>mean |t|</th></tr></thead>
              <tbody>
                {r.factors.map((f) => (
                  <tr key={f.factor}>
                    <td className="label">{f.factor}</td>
                    <td style={f.pct_days_t_gt2 < 1 / 3 ? { color: "#b07d2b" } : undefined}>
                      {pct(f.pct_days_t_gt2, 0)}</td>
                    <td>{num(f.mean_abs_t, 1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="muted small">
              The admission bar: a factor significant (|t| &gt; 2) on ≥ ⅓ of days earns its
              place; amber marks factors below it. {r.note.split(".")[3] ?? ""}
            </p>
          </div>
        )}
      </QueryState>

      <h2>Factor covariance — vols &amp; correlations</h2>
      <QueryState q={cq}>
        {(c) => (
          <div style={{ maxWidth: "52rem" }}>
            <div style={{ overflowX: "auto" }}>
              <table className="tufte" style={{ borderCollapse: "collapse" }}>
                <thead>
                  <tr>
                    <th className="label"></th>
                    {c.factors.map((f) => (
                      <th key={f} style={{ fontSize: "0.72rem" }}>{f.slice(0, 6)}</th>
                    ))}
                    <th>vol/d</th><th>vol 1y</th><th>ratio</th>
                  </tr>
                </thead>
                <tbody>
                  {c.factors.map((f, i) => {
                    const ratio = c.vol_full[f] > 0 ? c.vol_recent[f] / c.vol_full[f] : null;
                    return (
                      <tr key={f}>
                        <td className="label">{f}</td>
                        {c.factors.map((g, j) => (
                          <td key={g} className="num" style={{
                            textAlign: "center", fontSize: "0.72rem",
                            background: i === j ? undefined : corrBg(c.corr[i][j]),
                          }}>
                            {i === j ? "" : Math.round(c.corr[i][j] * 100)}
                          </td>
                        ))}
                        <td>{pct(c.vol_full[f], 2)}</td>
                        <td>{pct(c.vol_recent[f], 2)}</td>
                        <td style={ratio !== null && ratio > 1.25
                          ? { color: ratio > 1.5 ? "#a8322a" : "#b07d2b" } : undefined}>
                          {ratio === null ? "—" : num(ratio, 2)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <p className="muted small" style={{ marginTop: "0.3rem" }}>
              Correlations ×100, {c.n_days} days of history to {c.date}; shading = |ρ|.
              Avg |ρ| {num(c.avg_abs_corr.full, 2)} full vs {num(c.avg_abs_corr.recent, 2)} recent
              year. A vol ratio well above 1 (amber &gt; 1.25, red &gt; 1.5) is the
              vol-clustering warning — full-window bands understate that factor&rsquo;s current
              regime.
            </p>
          </div>
        )}
      </QueryState>

      <ExposureProfile />
      <FactorPortfolio />
    </main>
  );
}
