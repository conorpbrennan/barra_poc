// Attribution lens. Two tabs:
//   * Risk by level — the existing /attribution endpoint (risk BY Country/Sector/Issuer/Position).
//   * PnL attribution — Step 15 (/pnl_attribution + /residual + /linkage): realized PnL split into
//     factor + specific (Carino-linked), the residual diagnostics with RAG verdicts, and the §4
//     risk↔PnL reconcile band chart (base + stressed band per factor, dot = realized, z = surprise).
// The by-name drill lives in the Pivot via the cube measures (Factor contribution / Specific PnL /
// Realized PnL). Tufte/Few: grey + one accent, direct labels, colour only where it means something.
import { useMemo, useState } from "react";
import { useApp } from "../context/AppContext";
import {
  useAttribution, usePnlAttribution, usePnlResidual, usePnlLinkage,
} from "../api/hooks";
import type {
  PnlAttributionResult, PnlLinkageResult, PnlSeriesPoint,
} from "../api/types";
import { LabelBar } from "../components/svg";
import { QueryState, RagDot } from "../components/ui";
import { pct, signedPct, num, signedNum } from "../lib/format";

const LEVELS = ["sector", "issuer", "position", "country"];
const INK = "#111";
const BAND_KEYS: { key: keyof PnlSeriesPoint; label: string; color: string }[] = [
  { key: "market", label: "Market", color: "#c9c5bb" },
  { key: "style", label: "Style", color: "#3b5e8c" },
  { key: "specific", label: "Specific", color: "#8a867c" },
];

// ---- hero: cumulative stacked area (sign-aware) + realized ink line, direct-labelled ----
// (exported for tests)
export function StackedHero({ series, width = 640, height = 240 }: {
  series: PnlSeriesPoint[]; width?: number; height?: number;
}) {
  const pad = { l: 44, r: 78, t: 8, b: 18 };
  const iw = width - pad.l - pad.r, ih = height - pad.t - pad.b;
  const n = series.length;
  const { bands, yMin, yMax } = useMemo(() => {
    // stack positives up from zero and negatives down, per point, in band order
    const lo = new Array(n).fill(0), hi = new Array(n).fill(0);
    const bands = BAND_KEYS.map(({ key, label, color }) => {
      const lower: number[] = [], upper: number[] = [];
      series.forEach((p, i) => {
        const v = Number(p[key]) || 0;
        if (v >= 0) { lower.push(hi[i]); upper.push(hi[i] + v); hi[i] += v; }
        else { upper.push(lo[i]); lower.push(lo[i] + v); lo[i] += v; }
      });
      return { label, color, lower, upper };
    });
    const realized = series.map((p) => p.realized);
    const yMin = Math.min(0, ...lo, ...realized), yMax = Math.max(0, ...hi, ...realized);
    return { bands, yMin, yMax };
  }, [series, n]);
  if (n < 2) return <div className="muted small">insufficient data</div>;
  const span = yMax - yMin || 1;
  const sx = (i: number) => pad.l + (i / (n - 1)) * iw;
  const sy = (v: number) => pad.t + (1 - (v - yMin) / span) * ih;
  const areaPath = (lower: number[], upper: number[]) =>
    upper.map((v, i) => `${i ? "L" : "M"}${sx(i).toFixed(1)},${sy(v).toFixed(1)}`).join("") +
    lower.map((_, i) => {
      const j = n - 1 - i;
      return `L${sx(j).toFixed(1)},${sy(lower[j]).toFixed(1)}`;
    }).join("") + "Z";
  const linePath = series
    .map((p, i) => `${i ? "L" : "M"}${sx(i).toFixed(1)},${sy(p.realized).toFixed(1)}`).join("");
  // direct labels at the right edge; nudge apart if they collide
  const last = n - 1;
  const labels = [
    ...bands.map((b) => ({ text: b.label, color: b.color === "#c9c5bb" ? "#8a867c" : b.color,
                           y: sy((b.lower[last] + b.upper[last]) / 2) })),
    { text: `realized ${signedPct(series[last].realized, 1)}`, color: INK, y: sy(series[last].realized) },
  ].sort((a, b) => a.y - b.y);
  for (let i = 1; i < labels.length; i++) {
    if (labels[i].y - labels[i - 1].y < 12) labels[i].y = labels[i - 1].y + 12;
  }
  const ticks = [yMin, 0, yMax].filter((v, i, a) => a.indexOf(v) === i);
  return (
    <svg width={width} height={height} role="img" aria-label="cumulative PnL by source">
      {ticks.map((v) => (
        <g key={v}>
          <text x={pad.l - 6} y={sy(v) + 3.5} textAnchor="end" fontSize={10.5}
            fill="#6b6b63" className="num">{signedPct(v, 0)}</text>
          {v === 0 && <line x1={pad.l} x2={pad.l + iw} y1={sy(0)} y2={sy(0)}
            stroke="#d8d5cd" strokeWidth={1} strokeDasharray="2 2" />}
        </g>
      ))}
      {bands.map((b) => (
        <path key={b.label} d={areaPath(b.lower, b.upper)} fill={b.color} opacity={0.85} />
      ))}
      <path d={linePath} fill="none" stroke={INK} strokeWidth={1.4} />
      {labels.map((l) => (
        <text key={l.text} x={pad.l + iw + 5} y={l.y + 3.5} fontSize={11} fill={l.color}>{l.text}</text>
      ))}
      <text x={pad.l} y={height - 4} fontSize={10.5} fill="#6b6b63">{series[0].date}</text>
      <text x={pad.l + iw} y={height - 4} textAnchor="end" fontSize={10.5} fill="#6b6b63">
        {series[last].date}</text>
    </svg>
  );
}

// ---- §4 reconcile band chart: per row a base ±2σ band, a stressed band, and the realized dot.
// Each row reads against its OWN band, so x-position IS the surprise z (shared σ axis). ----
const DOT = { within: "#33332f", stress: "#b07d2b", investigate: "#a8322a" } as const;

export function BandChart({ lk, width = 700 }: { lk: PnlLinkageResult; width?: number }) {
  const rows = [...lk.rows, null, lk.book_total];   // null = separator
  const px = 52, cx = 350, labelX = 118, zX = width - 66;
  const rh = 30, y0 = 26;
  let y = y0;
  const els: JSX.Element[] = [];
  for (const r of rows) {
    if (r === null) {
      els.push(<line key="sep" x1={40} y1={y + 1} x2={zX + 50} y2={y + 1}
        stroke="#d8d5cd" strokeWidth={1} />);
      y += 12;
      continue;
    }
    const ratio = r.sd_base > 0 ? r.sd_stressed / r.sd_base : 1;
    const halfS = Math.min(2 * px * ratio, cx - 60);
    const z = r.z ?? 0;
    const dx = cx + Math.max(-4.4, Math.min(4.4, z)) * px;
    const c = DOT[r.verdict as keyof typeof DOT] ?? "#33332f";
    els.push(
      <g key={r.name}>
        <rect x={cx - halfS} y={y} width={2 * halfS} height={14} fill="#ece9e0" />
        <rect x={cx - 2 * px} y={y} width={4 * px} height={14} fill="#d9d5ca" />
        <text x={labelX} y={y + 11} textAnchor="end" fontSize={12.5}
          fontWeight={r.kind === "book" ? 600 : 400} fill="#2a2a26">{r.name}</text>
        <circle cx={dx} cy={y + 7} r={4.2} fill={c} />
        <text x={zX} y={y + 11} fontSize={11.5} fill={c} className="num">
          {signedNum(z, 1)}σ</text>
      </g>,
    );
    y += rh;
  }
  const axY = y + 2;
  const legendY = axY + 34;
  const height = legendY + 40;
  return (
    <svg width="100%" viewBox={`0 0 ${width} ${height}`} role="img"
      aria-label="realized contribution vs the start-of-period expected band, by factor"
      style={{ maxWidth: width }}>
      <line x1={cx} y1={y0 - 6} x2={cx} y2={axY - 6} stroke="#c9c5bb" strokeWidth={1}
        strokeDasharray="2 3" />
      {els}
      <g stroke="#c9c5bb" strokeWidth={1}>
        {[-3, -2, -1, 0, 1, 2, 3].map((s) => (
          <line key={s} x1={cx + s * px} y1={axY} x2={cx + s * px} y2={axY + 5} />
        ))}
      </g>
      {[-3, -2, -1, 0, 1, 2, 3].map((s) => (
        <text key={s} x={cx + s * px} y={axY + 16} textAnchor="middle" fontSize={10}
          fill="#6b6b63" className="num">{s === 0 ? "0" : `${s > 0 ? "+" : ""}${s}σ`}</text>
      ))}
      <rect x={70} y={legendY} width={20} height={10} fill="#d9d5ca" />
      <text x={95} y={legendY + 9} fontSize={10.5} fill="#2a2a26">
        base ±2σ (vols &amp; correlations at T)</text>
      <rect x={330} y={legendY} width={20} height={10} fill="#ece9e0" />
      <text x={355} y={legendY + 9} fontSize={10.5} fill="#2a2a26">
        stressed (vols ×{lk.stress.vol_mult}, ρ→1 blend {lk.stress.rho_blend})</text>
      <circle cx={80} cy={legendY + 27} r={4.2} fill={DOT.within} />
      <text x={90} y={legendY + 31} fontSize={10.5} fill="#2a2a26">within — as expected</text>
      <circle cx={260} cy={legendY + 27} r={4.2} fill={DOT.stress} />
      <text x={270} y={legendY + 31} fontSize={10.5} fill="#2a2a26">beyond base — stress regime</text>
      <circle cx={470} cy={legendY + 27} r={4.2} fill={DOT.investigate} />
      <text x={480} y={legendY + 31} fontSize={10.5} fill="#2a2a26">beyond stressed — investigate</text>
    </svg>
  );
}

type Preset = "t12m" | "ytd" | "inception" | "custom";

function PnlTab() {
  const base = usePnlAttribution();          // default window; also supplies the calendar bounds
  const cal = base.data?.calendar;
  const [preset, setPreset] = useState<Preset>("t12m");
  const [customFrom, setCustomFrom] = useState("");
  const [customTo, setCustomTo] = useState("");
  const [horizon, setHorizon] = useState(3);
  const from =
    preset === "ytd" && cal ? `${cal.max.slice(0, 4)}-01-01` :
    preset === "inception" && cal ? cal.min :
    preset === "custom" && customFrom ? customFrom : undefined;
  const to = preset === "custom" && customTo ? customTo : undefined;
  const q = usePnlAttribution(from, to);
  const rq = usePnlResidual(from, to);
  const lq = usePnlLinkage(horizon);

  return (
    <>
      <div className="row" style={{ marginBottom: "0.8rem", flexWrap: "wrap" }}>
        {([["t12m", "Trailing 12m"], ["ytd", "YTD"], ["inception", "Since inception"],
           ["custom", "Custom"]] as [Preset, string][]).map(([k, lab]) => (
          <button key={k} className={preset === k ? "primary" : ""}
            onClick={() => setPreset(k)}>{lab}</button>
        ))}
        {preset === "custom" && cal && (
          <>
            <input type="date" value={customFrom} min={cal.min} max={cal.max}
              onChange={(e) => setCustomFrom(e.target.value)} />
            <span className="muted small">→</span>
            <input type="date" value={customTo} min={cal.min} max={cal.max}
              onChange={(e) => setCustomTo(e.target.value)} />
          </>
        )}
      </div>

      <QueryState q={q}>
        {(a: PnlAttributionResult) => (
          <div style={{ maxWidth: "52rem" }}>
            <p style={{ margin: "0 0 0.6rem" }}>
              <strong style={{ fontSize: "1.25rem" }} className="num">
                {signedPct(a.headline.realized_geometric, 1)}
              </strong>{" "}
              <span className="muted small">
                realized (geometric) · {a.from} → {a.to} · {a.n_days} trading days —
                factor {signedPct(a.headline.factor, 1)}, specific {signedPct(a.headline.specific, 1)}
                {a.headline.specific_share !== null &&
                  ` (${signedPct(a.headline.specific_share, 0)} of total)`}
              </span>
            </p>
            <StackedHero series={a.series} />
            <p className="muted small" style={{ margin: "0.2rem 0 1rem" }}>
              Cumulative arithmetic contributions — where the money came from. The ink line is
              realized (= Market + Style + Specific, an identity). Price-only; drifting buy-and-hold
              weights between 13F filings. Drill factor→name in the Pivot with the
              <code> Factor contribution</code> / <code>Specific PnL</code> /
              <code> Realized PnL</code> measures.
            </p>
            <table className="tufte">
              <thead>
                <tr><th className="label">Factor</th><th>Avg exposure</th>
                  <th>Cum factor return</th><th>Contribution</th><th>% of total</th><th>t-stat</th></tr>
              </thead>
              <tbody>
                {a.factors.map((r) => (
                  <tr key={r.factor}>
                    <td className="label">{r.factor}</td>
                    <td>{num(r.avg_exposure, 2)}</td>
                    <td>{signedPct(r.cum_factor_return, 1)}</td>
                    <td>{signedPct(r.contribution, 2)}</td>
                    <td>{r.pct_of_total === null ? "—" : pct(r.pct_of_total, 0)}</td>
                    <td>{num(r.t_stat, 1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="muted small">
              Coverage: {a.coverage.mean_priced_share === null ? "—"
                : pct(a.coverage.mean_priced_share, 0)} of book weight priced on average
              {a.coverage.unpriced.length
                ? ` · unpriced (${a.coverage.unpriced.length}): ${a.coverage.unpriced.slice(0, 8)
                    .map((u) => `${u.name.toUpperCase()} (${pct(u.weight, 1)})`).join(", ")}`
                : " · every held name priced"}.
            </p>
          </div>
        )}
      </QueryState>

      <h2>Residual diagnostics</h2>
      <QueryState q={rq}>
        {(r) => (
          <div style={{ maxWidth: "52rem" }}>
            <table className="tufte">
              <tbody>
                {r.checks.map((c) => (
                  <tr key={c.name}>
                    <td style={{ width: "1.4rem" }}><RagDot status={c.status} /></td>
                    <td className="label">{c.name}</td>
                    <td>{signedNum(c.value, 2)}</td>
                    <td className="muted">{c.verdict}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="muted small" style={{ marginTop: "0.4rem" }}>
              {r.specific_share !== null && <>specific share {signedPct(r.specific_share, 0)} · </>}
              {r.explained_share !== null &&
                <>factors explain {pct(r.explained_share, 0)} of monthly variance · </>}
              {r.concentration.hhi !== null &&
                <>residual concentration HHI {num(r.concentration.hhi, 3)} (top-5{" "}
                  {pct(r.concentration.top5_share, 0)}) · </>}
              hit rate {pct(r.hit_rate.names, 0)} of names / {pct(r.hit_rate.months, 0)} of months
            </p>
            {r.factor_regression.loadings.length > 0 && (
              <p className="muted small">
                Residual-vs-factor loadings (largest |t|):{" "}
                {r.factor_regression.loadings.slice(0, 3)
                  .map((l) => `${l.factor} β=${signedNum(l.beta, 2)} (t=${signedNum(l.t_stat, 1)})`)
                  .join(", ")}
              </p>
            )}
            <p className="muted small">{r.note}</p>
          </div>
        )}
      </QueryState>

      <h2>Risk ↔ PnL reconcile</h2>
      <div className="row" style={{ marginBottom: "0.5rem" }}>
        <span className="muted small">Horizon</span>
        <select value={horizon} onChange={(e) => setHorizon(Number(e.target.value))}>
          {[1, 3, 6, 12].map((h) => <option key={h} value={h}>{h}m</option>)}
        </select>
      </div>
      <QueryState q={lq}>
        {(lk) => (
          <div style={{ maxWidth: "52rem" }}>
            <p className="muted small" style={{ margin: "0 0 0.4rem" }}>
              Risk decomposition at <strong>{lk.T}</strong> vs realized PnL over{" "}
              <strong>{lk.T} → {lk.to}</strong> ({lk.n_days} trading days). Each row reads against
              its own start-of-period band; the dot is the realized contribution.
            </p>
            <BandChart lk={lk} />
            {lk.surprises.length > 0 && (
              <p style={{ marginTop: "0.4rem" }}>
                <strong>Investigate:</strong>{" "}
                {lk.surprises.map((s) =>
                  `${s.name} (${signedPct(s.realized, 1)} vs ±${pct(2 * s.sd_stressed, 1)} stressed)`)
                  .join(", ")}
              </p>
            )}
            {lk.positions.length > 0 && (
              <>
                <p className="muted small" style={{ margin: "0.8rem 0 0.2rem" }}>
                  Position surprises — realized vs own ex-ante σ at T (top |z|)
                </p>
                <table className="tufte">
                  <thead><tr><th className="label">Name</th><th>Weight</th><th>Realized</th>
                    <th>z</th><th>Verdict</th></tr></thead>
                  <tbody>
                    {lk.positions.map((p) => (
                      <tr key={p.position}>
                        <td className="label">{p.name.toUpperCase()}</td>
                        <td>{pct(p.weight, 1)}</td>
                        <td>{signedPct(p.realized, 2)}</td>
                        <td style={{ color: DOT[p.verdict as keyof typeof DOT] ?? INK }}>
                          {signedNum(p.z, 1)}</td>
                        <td className="muted">{p.verdict}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}
            <p className="muted small" style={{ marginTop: "0.4rem" }}>{lk.note}</p>
          </div>
        )}
      </QueryState>
    </>
  );
}

export function Attribution() {
  const { date, scenario } = useApp();
  const [by, setBy] = useState("sector");
  const [tab, setTab] = useState<"risk" | "pnl">("risk");
  const q = useAttribution(date, scenario, by);

  return (
    <main className="lens">
      <h1>Attribution</h1>
      <p className="sub">
        {tab === "risk"
          ? `Standalone risk by level · ${scenario} · as-of ${date}`
          : "Realized PnL by factor + residual · risk↔PnL reconcile"}
      </p>

      <div className="row" style={{ marginBottom: "0.8rem" }}>
        <button className={tab === "risk" ? "primary" : ""} onClick={() => setTab("risk")}>Risk by level</button>
        <button className={tab === "pnl" ? "primary" : ""} onClick={() => setTab("pnl")}>PnL attribution</button>
      </div>

      {tab === "pnl" ? (
        <PnlTab />
      ) : (
        <>
          <div className="row" style={{ marginBottom: "0.8rem" }}>
            <span className="muted small">Level</span>
            <select value={by} onChange={(e) => setBy(e.target.value)}>
              {LEVELS.map((l) => <option key={l} value={l}>{l}</option>)}
            </select>
          </div>
          <QueryState q={q}>
            {(rows) => {
              const key = by === "position" ? "Position" : by[0].toUpperCase() + by.slice(1);
              const sorted = [...rows].sort(
                (a, b) => Number(b["Scenario VaR 99"] ?? 0) - Number(a["Scenario VaR 99"] ?? 0));
              const maxV = Math.max(...sorted.map((r) => Number(r["Scenario VaR 99"] ?? 0)), 1e-6);
              return (
                <div style={{ maxWidth: "52rem" }}>
                  {sorted.slice(0, 15).map((r, i) => (
                    <LabelBar key={i} label={String(r[by === "position" ? "Ticker" : key] ?? r[key] ?? "—")}
                      value={Number(r["Scenario VaR 99"] ?? 0)} max={maxV} suffix="" />
                  ))}
                  <table className="tufte" style={{ marginTop: "1rem" }}>
                    <thead><tr><th className="label">{key}</th><th>Net exposure</th><th>Scenario VaR 99</th><th>Worst loss</th></tr></thead>
                    <tbody>
                      {sorted.map((r, i) => (
                        <tr key={i}>
                          <td className="label">{String(r[by === "position" ? "Ticker" : key] ?? r[key] ?? "—")}</td>
                          <td>{num(Number(r["Net exposure"] ?? 0), 2)}</td>
                          <td>{pct(Number(r["Scenario VaR 99"] ?? 0))}</td>
                          <td>{pct(Number(r["Scenario worst loss"] ?? 0))}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              );
            }}
          </QueryState>
        </>
      )}
    </main>
  );
}
