// Attribution lens. Two tabs:
//   * Risk by level — the existing /attribution endpoint (risk BY Country/Sector/Issuer/Position).
//   * PnL attribution — Step 15 (/pnl_attribution + /residual + /linkage): realized PnL split into
//     factor + specific (Carino-linked), the residual diagnostics with RAG verdicts, and the §4
//     risk↔PnL reconcile band chart (base + stressed band per factor, dot = realized, z = surprise).
// The by-name drill lives in the Pivot via the cube measures (Factor contribution / Specific PnL /
// Realized PnL). Tufte/Few: grey + one accent, direct labels, colour only where it means something.
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useApp } from "../context/AppContext";
import {
  useContributions, useMeta, usePivot, usePnlAttribution, usePnlResidual, usePnlLinkage,
  usePnlNames,
} from "../api/hooks";
import type {
  ContributionsResult, PnlAttributionResult, PnlLinkagePosition, PnlLinkageResult,
  PnlLinkageRow, PnlSeriesPoint,
} from "../api/types";
import { TipBox, svgPoint } from "../components/svg";
import { HowToRead, QueryState, RagDot } from "../components/ui";
import { pct, signedPct, num, signedNum } from "../lib/format";

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
  const [hov, setHov] = useState<number | null>(null);
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
  const hp = hov !== null ? series[hov] : null;
  return (
    <svg width={width} height={height} role="img" aria-label="cumulative PnL by source"
      onMouseMove={(e) => {
        const p = svgPoint(e);
        if (!p) return;
        const i = Math.round(((p.x - pad.l) / iw) * (n - 1));
        setHov(Math.max(0, Math.min(n - 1, i)));
      }}
      onMouseLeave={() => setHov(null)}>
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
      {hp && hov !== null && (
        <>
          <line x1={sx(hov)} x2={sx(hov)} y1={pad.t} y2={pad.t + ih}
            stroke="#c9c5bb" strokeWidth={0.8} strokeDasharray="2 2" pointerEvents="none" />
          <circle cx={sx(hov)} cy={sy(hp.realized)} r={2.6} fill={INK} pointerEvents="none" />
          <TipBox x={sx(hov)} y={sy(hp.realized)} width={width} height={height}
            lines={[hp.date,
                    `realized ${signedPct(hp.realized, 2)}`,
                    `market ${signedPct(hp.market, 2)}`,
                    `style ${signedPct(hp.style, 2)}`,
                    `specific ${signedPct(hp.specific, 2)}`]} />
        </>
      )}
    </svg>
  );
}

// ---- §4 reconcile band chart: per row a base ±2σ band, a stressed band, and the realized dot.
// Each row reads against its OWN band, so x-position IS the surprise z (shared σ axis). ----
const DOT = { within: "#33332f", stress: "#b07d2b", investigate: "#a8322a" } as const;

export function BandChart({ lk, width = 700, onDot }: {
  lk: PnlLinkageResult; width?: number;
  onDot?: (name: string) => void;      // linked selection: a factor dot opens its drill drawer
}) {
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
    // an exposure-migration breach is a band artifact (x frozen at T), not a factor event —
    // draw it hollow so it doesn't read like a real breach
    const hollow = r.driver?.kind === "exposure_migration";
    const drillable = onDot && r.kind === "factor";
    els.push(
      <g key={r.name}>
        <rect x={cx - halfS} y={y} width={2 * halfS} height={14} fill="#ece9e0" />
        <rect x={cx - 2 * px} y={y} width={4 * px} height={14} fill="#d9d5ca" />
        <text x={labelX} y={y + 11} textAnchor="end" fontSize={12.5}
          fontWeight={r.kind === "book" ? 600 : 400} fill="#2a2a26">{r.name}</text>
        <g data-dot={drillable ? r.name : undefined}
          onClick={drillable ? () => onDot(r.name) : undefined}
          style={drillable ? { cursor: "pointer" } : undefined}>
          {/* transparent halo so the 4px dot has a clickable target */}
          {drillable && <circle cx={dx} cy={y + 7} r={11} fill="transparent" />}
          <circle cx={dx} cy={y + 7} r={4.2} fill={hollow ? "#fffff8" : c}
            stroke={hollow ? c : "none"} strokeWidth={hollow ? 1.5 : 0}>
            <title>{`${r.name}: realized ${signedPct(r.realized, 2)} · z ${signedNum(z, 1)}σ · `
              + `base ±${pct(2 * r.sd_base, 2)} · stressed ±${pct(2 * r.sd_stressed, 2)}`
              + ` · ${r.verdict}${drillable ? " · click to drill" : ""}`}</title>
          </circle>
        </g>
        <text x={zX} y={y + 11} fontSize={11.5} fill={c} className="num">
          {signedNum(z, 1)}σ</text>
      </g>,
    );
    y += rh;
  }
  const axY = y + 2;
  const legendY = axY + 34;
  const height = legendY + 58;
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
      <circle cx={80} cy={legendY + 45} r={4.2} fill="#fffff8" stroke="#33332f" strokeWidth={1.5} />
      <text x={90} y={legendY + 49} fontSize={10.5} fill="#2a2a26">
        hollow — exposure migrated in-window (band artifact, see driver read)</text>
    </svg>
  );
}

// ---- §4 drill drawers: click a breach row (or a factor dot) → the decomposition behind it,
// served by the same guarded /pivot the grid uses (no new endpoint, no off-allowlist number).
// Tufte: inline, hairline-bounded, one open at a time; bidirectional bars, direct-labelled,
// the accent marks only the largest contributor (colour = meaning). ----

// pure, exported for tests — bidirectional magnitude bars around a zero centreline
export function DrillBars({ bars }: {
  bars: { label: string; v: number; note?: string }[];
}) {
  if (!bars.length) return <p className="muted small">nothing to show</p>;
  const max = Math.max(...bars.map((b) => Math.abs(b.v)), 1e-12);
  const W = 150, C = W / 2;
  const iBig = bars.reduce((a, b, i) => (Math.abs(b.v) > Math.abs(bars[a].v) ? i : a), 0);
  return (
    <div style={{ margin: "0.35rem 0" }}>
      {bars.map((b, i) => {
        const w = Math.max(1, (Math.abs(b.v) / max) * (C - 2));
        return (
          <div key={b.label} className="row small" style={{ gap: "0.55rem", margin: "0.12rem 0" }}>
            <span className="label" style={{ width: "6.4rem", textAlign: "right", flex: "none" }}>
              {b.label}</span>
            <span style={{ position: "relative", width: W, height: 9, flex: "none" }}>
              <span style={{ position: "absolute", left: C, top: -1, bottom: -1,
                borderLeft: "1px solid #d8d5cd" }} />
              <span data-bar={b.label} style={{ position: "absolute", top: 0, height: 9,
                left: b.v < 0 ? C - w : C, width: w,
                background: i === iBig ? "#3b5e8c" : "#c9c5bb" }} />
            </span>
            <span className="num">{signedPct(b.v, 2)}</span>
            {b.note && <span className="muted">{b.note}</span>}
          </div>
        );
      })}
    </div>
  );
}

// deep link into the Pivot lens carrying this exact slice (?drill= applied once on mount there)
const pivotDrill = (cfg: { rows: string[]; measures: string[];
  filters: Record<string, string[]> }) =>
  `/pivot?drill=${encodeURIComponent(JSON.stringify(cfg))}`;

const winDates = (dates: string[], T: string, to: string) =>
  dates.filter((d) => d >= T && d < to);   // fwd-month convention: value at d covers d → d+1m

// one name's breach explained: per-factor contribution over the window + the T loadings
function PositionDrawer({ p, lk, dates }: {
  p: PnlLinkagePosition; lk: PnlLinkageResult; dates: string[];
}) {
  const win = winDates(dates, lk.T, lk.to);
  const base = { Book: [lk.book], Position: [p.position] };
  const cq = usePivot("Factor", "", "Factor contribution",
    JSON.stringify({ ...base, Date: win }), false, win.length > 0);
  const lq = usePivot("Factor", "", "Net exposure",
    JSON.stringify({ ...base, Date: [lk.T] }), false, win.length > 0);
  const nFactors = lk.rows.filter((r) => r.kind === "factor").length;
  return (
    <div style={{ padding: "0.4rem 0 0.55rem 1.35rem" }}>
      <p className="small" style={{ margin: 0 }}>
        realized <strong className="num">{signedPct(p.realized, 2)}</strong> = factor{" "}
        {signedPct(p.factor_pnl, 2)} + specific {signedPct(p.specific_pnl, 2)} · band
        ±{pct(2 * p.sd_base, 2)} · z {signedNum(p.z, 1)}σ · {lk.T} → {lk.to}
      </p>
      <QueryState q={cq}>
        {(d) => {
          const loads = new Map((lq.data?.records ?? [])
            .filter((r) => typeof r["Net exposure"] === "number")
            .map((r) => [String(r.Factor), (r["Net exposure"] as number) / (p.weight || 1)]));
          const bars = d.records
            .map((r) => ({ f: String(r.Factor),
              v: typeof r["Factor contribution"] === "number" ? (r["Factor contribution"] as number) : 0 }))
            .filter((b) => b.v !== 0)
            .sort((a, b) => Math.abs(b.v) - Math.abs(a.v))
            .map((b) => ({ label: b.f, v: b.v,
              note: loads.has(b.f) ? `loading ${num(loads.get(b.f)!, 2)} at T`
                                   : "no loading at T" }));
          bars.push({ label: "Specific", v: p.specific_pnl,
            note: p.specific_pnl === 0 ? "no residual history" : "" });
          return (
            <>
              <DrillBars bars={bars} />
              {loads.size < nFactors && (
                <p className="muted small" style={{ margin: "0.15rem 0 0" }}>
                  only {loads.size} of {nFactors} factor loadings at {lk.T} — a thin loading
                  set is itself a hidden-beta suspect.
                </p>
              )}
              {p.driver && (
                <p className="small" style={{ margin: "0.3rem 0 0", maxWidth: "38rem" }}>
                  {p.driver.text}
                </p>
              )}
              <p className="small" style={{ margin: "0.3rem 0 0" }}>
                <Link className="muted" to={pivotDrill({ rows: ["Factor"],
                  measures: ["Factor contribution", "Specific PnL", "Realized PnL"],
                  filters: { ...base, Date: win } })}>open in Pivot →</Link>
              </p>
            </>
          );
        }}
      </QueryState>
    </div>
  );
}

// one positions-table row + its optional expansion (indentation + hairlines, no container)
function FragRow({ row: p, open, onToggle, lk, dates }: {
  row: PnlLinkagePosition; open: boolean; onToggle: () => void;
  lk: PnlLinkageResult; dates: string[];
}) {
  return (
    <>
      <tr onClick={onToggle} style={{ cursor: "pointer" }}>
        <td className="label">
          <span className="muted" style={{ display: "inline-block", width: "0.9rem",
            transform: open ? "rotate(90deg)" : undefined, transition: "transform 0.1s" }}>
            ▸</span>
          {p.name.toUpperCase()}
        </td>
        <td>{pct(p.weight, 1)}</td>
        <td>{signedPct(p.realized, 2)}</td>
        <td className="muted">{signedPct(p.factor_pnl, 2)}</td>
        <td className="muted">{signedPct(p.specific_pnl, 2)}</td>
        <td style={{ color: DOT[p.verdict as keyof typeof DOT] ?? INK }}>
          {signedNum(p.z, 1)}</td>
        <td className="muted">{p.verdict}</td>
      </tr>
      {open && (
        <tr>
          <td colSpan={7} style={{ padding: 0 }}>
            <PositionDrawer p={p} lk={lk} dates={dates} />
          </td>
        </tr>
      )}
    </>
  );
}

// the inverse read for a factor row: which names carried this factor's move
function FactorDrawer({ row, lk, dates, onClose }: {
  row: PnlLinkageRow; lk: PnlLinkageResult; dates: string[]; onClose: () => void;
}) {
  const win = winDates(dates, lk.T, lk.to);
  const q = usePivot("Issuer", "", "Factor contribution",
    JSON.stringify({ Book: [lk.book], Factor: [row.name], Date: win }), false, win.length > 0);
  return (
    <div style={{ borderTop: "1px solid #d8d5cd", borderBottom: "1px solid #d8d5cd",
      padding: "0.45rem 0 0.55rem", margin: "0.3rem 0 0.5rem" }}>
      <p className="small" style={{ margin: 0 }}>
        <strong>{row.name}</strong> — who carried it: realized{" "}
        <strong className="num">{signedPct(row.realized, 2)}</strong> · band
        ±{pct(2 * row.sd_base, 2)} · z {signedNum(row.z ?? 0, 1)}σ · {lk.T} → {lk.to}{" "}
        <button className="muted" style={{ marginLeft: "0.6rem" }} onClick={onClose}>×</button>
      </p>
      <QueryState q={q}>
        {(d) => {
          const rows = d.records
            .map((r) => ({ label: String(r.Issuer),
              v: typeof r["Factor contribution"] === "number" ? (r["Factor contribution"] as number) : 0 }))
            .filter((b) => b.v !== 0)
            .sort((a, b) => Math.abs(b.v) - Math.abs(a.v));
          const top = rows.slice(0, 8);
          const rest = rows.slice(8).reduce((s, b) => s + b.v, 0);
          const bars = [...top, ...(rows.length > 8
            ? [{ label: `…${rows.length - 8} more`, v: rest }] : [])];
          return (
            <>
              <DrillBars bars={bars} />
              {row.driver && (
                <p className="small" style={{ margin: "0.3rem 0 0", maxWidth: "38rem" }}>
                  {row.driver.text}
                </p>
              )}
              <p className="small" style={{ margin: "0.3rem 0 0" }}>
                <Link className="muted" to={pivotDrill({ rows: ["Issuer"],
                  measures: ["Factor contribution"],
                  filters: { Book: [lk.book], Factor: [row.name], Date: win } })}>
                  open in Pivot →</Link>
              </p>
            </>
          );
        }}
      </QueryState>
    </div>
  );
}

// Significance of the specific (stock-selection) stream: t = IR·√years observed, and the years
// of data an IR of this size needs to clear two standard errors ((2/IR)²). Exported for tests.
export function irSignificance(ir: number | null | undefined, nMonths: number) {
  if (ir == null || !isFinite(ir) || nMonths <= 0) return null;
  const t = ir * Math.sqrt(nMonths / 12);
  const years = ir === 0 ? null : (2 / Math.abs(ir)) ** 2;
  return { t, years };
}

// ---- residual explorer: the specific PnL name by name (winners / losers + persistence) ----
function ResidualExplorer({ from, to }: { from?: string; to?: string }) {
  const q = usePnlNames(from, to);
  const cols = (
    <thead><tr><th className="label">Name</th><th>Specific</th><th>Factor</th>
      <th>Persistence</th><th>Months +</th></tr></thead>
  );
  const row = (r: import("../api/types").PnlNameRow) => (
    <tr key={r.position}>
      <td className="label">{r.ticker.toUpperCase()}</td>
      <td>{signedPct(r.specific_pnl, 1)}</td>
      <td className="muted">{signedPct(r.factor_pnl, 1)}</td>
      <td style={r.sign_persistence !== null && r.sign_persistence > 0.7
        ? { color: "#b07d2b" } : undefined}>
        {r.sign_persistence === null ? "—" : pct(r.sign_persistence, 0)}</td>
      <td className="muted">{r.hit_rate === null ? "—" : pct(r.hit_rate, 0)}</td>
    </tr>
  );
  return (
    <>
      <h2>Residual explorer — the specific PnL, name by name</h2>
      <QueryState q={q}>
        {(n) => (
          <div style={{ maxWidth: "52rem" }}>
            <div className="row" style={{ gap: "2.5rem", alignItems: "flex-start" }}>
              <table className="tufte" style={{ minWidth: "22rem" }}>
                {cols}<tbody>{n.winners.slice(0, 8).map(row)}</tbody>
              </table>
              <table className="tufte" style={{ minWidth: "22rem" }}>
                {cols}<tbody>{n.losers.slice(0, 8).map(row)}</tbody>
              </table>
            </div>
            <p className="muted small" style={{ marginTop: "0.4rem" }}>
              Persistence = share of consecutive months with the same specific sign: ≈50% is
              memoryless (re-underwritten bets); well above (amber &gt; 70%) is a persistent
              unexplained driver — a real edge, a stale 13F weight, or a missing factor.
            </p>
          </div>
        )}
      </QueryState>
    </>
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
  const meta = useMeta();
  // one drill open at a time (accordion) — a chart dot opens a factor drawer, a table row a
  // position drawer; opening either closes the other so the overview stays scannable.
  const [drill, setDrill] = useState<{ type: "factor" | "position"; key: string } | null>(null);

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
                  ` (${signedPct(a.headline.specific_share, 0)} of total)`} ·
                Cariño-linked, parts sum to the geometric return exactly
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
            <p className="muted small" style={{ margin: "0.3rem 0 0" }}>
              Each factor line is x·f — the return on the pure-factor portfolio the book
              implicitly held. Contributions are Cariño-linked so they sum exactly to the
              geometric period return; the chart above is the unlinked arithmetic path.
            </p>
            <p className="muted small">
              Coverage: {a.coverage.mean_priced_share === null ? "—"
                : pct(a.coverage.mean_priced_share, 0)} of book weight priced on average
              {a.coverage.unpriced.length
                ? ` · unpriced (${a.coverage.unpriced.length}): ${a.coverage.unpriced.slice(0, 8)
                    .map((u) => `${u.name.toUpperCase()} (${pct(u.weight, 1)})`).join(", ")}`
                : " · every held name priced"}.
            </p>
            <p className="muted small">
              Absolute attribution — no benchmark, so Market stands in for beta (a
              Brinson allocation/selection view needs one). Model-conditional: the factor lines
              mean <em>this</em> model&rsquo;s factor definitions. 13F weights are quarterly, so
              intra-period trading and exposure timing fold into Specific.
            </p>
          </div>
        )}
      </QueryState>

      <h2>Residual diagnostics</h2>
      <p className="muted small" style={{ margin: "0 0 0.5rem" }}>
        The tilt-vs-skill split: the factor contributions above are tilt — replicable with
        factor products; the specific stream is stock selection, the part the model
        can&rsquo;t span.
      </p>
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
            {(() => {
              const ir = r.checks.find((c) => c.name.startsWith("Information ratio"))?.value;
              const sig = irSignificance(ir ?? null, r.n_months);
              if (!sig) return null;
              return (
                <p className="muted small" style={{ marginTop: "0.4rem" }}>
                  Significance t = IR·√T ≈ {signedNum(sig.t, 1)} over {r.n_months} months
                  {sig.years !== null && sig.years < 200 &&
                    <> · an IR of {num(Math.abs(ir!), 2)} needs ≈{num(sig.years, 0)}y of data to
                      clear 2σ — a single good quarter is noise</>}.
                </p>
              );
            })()}
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

      <ResidualExplorer from={from} to={to} />

      <h2>Risk ↔ PnL reconcile</h2>
      <div className="row" style={{ marginBottom: "0.5rem" }}>
        <span className="muted small">Horizon</span>
        <select value={horizon}
          onChange={(e) => { setHorizon(Number(e.target.value)); setDrill(null); }}>
          {[1, 3, 6, 12].map((h) => <option key={h} value={h}>{h}m</option>)}
        </select>
      </div>
      <QueryState q={lq}>
        {(lk) => (
          <div style={{ maxWidth: "52rem" }}>
            <p className="muted small" style={{ margin: "0 0 0.4rem" }}>
              Risk decomposition at <strong>{lk.T}</strong> vs realized PnL over{" "}
              <strong>{lk.T} → {lk.to}</strong> ({lk.n_days} trading days). Each row reads against
              its own start-of-period band; the dot is the realized contribution — click a factor
              dot to see which names carried it.
            </p>
            <BandChart lk={lk} onDot={(name) =>
              setDrill((d) => d?.type === "factor" && d.key === name
                ? null : { type: "factor", key: name })} />
            {drill?.type === "factor" && (() => {
              const row = lk.rows.find((r) => r.name === drill.key);
              return row ? <FactorDrawer row={row} lk={lk} dates={meta.data?.dates ?? []}
                onClose={() => setDrill(null)} /> : null;
            })()}
            {(() => {
              const flagged = [...lk.rows, lk.book_total].filter((r) => r.driver);
              if (!flagged.length) return null;
              return (
                <div style={{ marginTop: "0.4rem" }}>
                  <p style={{ margin: 0 }}><strong>Outside the base band — driver read:</strong></p>
                  {flagged.map((r) => (
                    <p key={r.name} className="small" style={{ margin: "0.25rem 0 0" }}>
                      <strong>{r.name}</strong> ({signedNum(r.z ?? 0, 1)}σ,{" "}
                      {r.driver!.kind.replace(/_/g, " ")}) — {r.driver!.text}
                    </p>
                  ))}
                </div>
              );
            })()}
            {lk.positions.length > 0 && (
              <>
                <p className="muted small" style={{ margin: "0.8rem 0 0.2rem" }}>
                  Position surprises — realized vs own ex-ante σ at T (top |z|), split
                  factor / specific · click a row to drill the driver
                </p>
                <table className="tufte">
                  <thead><tr><th className="label">Name</th><th>Weight</th><th>Realized</th>
                    <th>Factor</th><th>Specific</th><th>z</th><th>Verdict</th></tr></thead>
                  <tbody>
                    {lk.positions.map((p) => {
                      const open = drill?.type === "position" && drill.key === p.position;
                      return (
                        <FragRow key={p.position} open={open}
                          onToggle={() => setDrill(open ? null
                            : { type: "position", key: p.position })}
                          row={p} lk={lk} dates={meta.data?.dates ?? []} />
                      );
                    })}
                  </tbody>
                </table>
                {(() => {
                  const flagged = lk.positions.filter((p) => p.driver);
                  if (!flagged.length) return null;
                  return (
                    <div style={{ marginTop: "0.4rem" }}>
                      <p style={{ margin: 0 }}>
                        <strong>Outside the band — stock-level driver read:</strong>
                      </p>
                      {flagged.map((p) => (
                        <p key={p.position} className="small" style={{ margin: "0.25rem 0 0" }}>
                          <strong>{p.name.toUpperCase()}</strong> ({signedNum(p.z, 1)}σ,{" "}
                          {p.driver!.kind.replace(/_/g, " ")}
                          {p.driver!.hidden_beta ? " · hidden beta" : ""}) — {p.driver!.text}
                        </p>
                      ))}
                    </div>
                  );
                })()}
                {lk.breach_comovement && (
                  <p className="small" style={{ marginTop: "0.4rem",
                    color: lk.breach_comovement.verdict === "common_thread" ? "#a8322a" : undefined }}>
                    <strong>Breach co-movement
                      ({lk.breach_comovement.names.map((n) => n.toUpperCase()).join(", ")}):</strong>{" "}
                    {lk.breach_comovement.text}
                    {lk.breach_comovement.shared_sector &&
                      ` Shared sector: ${lk.breach_comovement.shared_sector}.`}
                  </p>
                )}
              </>
            )}
            <p className="muted small" style={{ marginTop: "0.4rem" }}>{lk.note}</p>
          </div>
        )}
      </QueryState>
    </>
  );
}

// ---- Euler contributions tab: CTR (positions, vol units) + CTV (factors, variance units) ----
function EulerTab() {
  const { date, book } = useApp();
  const q = useContributions(date, book);
  return (
    <QueryState q={q}>
      {(c: ContributionsResult) => (
        <div style={{ maxWidth: "52rem" }}>
          <p style={{ margin: "0 0 0.6rem" }}>
            <strong style={{ fontSize: "1.25rem" }} className="num">{pct(c.vol_1d, 2)}</strong>{" "}
            <span className="muted small">
              book daily vol (model, σ² = x&prime;Fx + w&prime;Δw) ·
              factor {c.factor_share === null ? "—" : pct(c.factor_share, 0)} of variance,
              specific {c.factor_share === null ? "—" : pct(1 - c.factor_share, 0)} ·
              normal-approx VaR99 {pct(c.var99_normal, 2)}
            </span>
          </p>

          <HowToRead>
            Both tables recover the same {pct(c.vol_1d, 2)} book vol, but in different units —
            that is the one trap here. The <em>position</em> table is the direct read: CTR = w·MCR
            is an exact Euler split of σ, so the CTR column simply sums to
            {" "}{pct(c.vol_1d, 2)}. The <em>factor</em> table works in variance: CTV_k = x_k·(Fx)_k
            sums to factor variance x&prime;Fx, not vol — to recover σ, add back the specific block
            and take the square root:
            {" "}<code>√(Σ CTV + specific) = √({signedNum(c.sum_ctv * 1e4, 2)} +
            {" "}{signedNum(c.specific_variance * 1e4, 2)} bp²) = {pct(c.vol_1d, 2)}</code>
            {" "}(the recovery row under the table). Never compare a CTR with a CTV directly —
            vol units vs variance units. Cross-factor covariance is split 50/50 inside CTV, so a
            negative CTV line is a genuine hedge; MCR is a rate (risk per unit weight), nothing
            to sum. All of it is model vol on the full factor-return history — distinct from the
            scenario-VaR views.
          </HowToRead>

          <h2>Factors — contribution to variance (CTV)</h2>
          <table className="tufte">
            <thead><tr><th className="label">Factor</th><th>Exposure</th><th>CTV</th>
              <th>% of variance</th></tr></thead>
            <tbody>
              {c.factors.map((r) => (
                <tr key={r.factor}>
                  <td className="label">{r.factor}</td>
                  <td>{num(r.exposure, 2)}</td>
                  <td className={r.ctv < 0 ? "rag-green" : ""}>{signedNum(r.ctv * 1e4, 2)}bp²</td>
                  <td>{r.pct_of_variance === null ? "—" : pct(r.pct_of_variance, 1)}</td>
                </tr>
              ))}
              <tr style={{ fontWeight: 600 }}>
                <td className="label">Σ factors</td><td></td>
                <td>{signedNum(c.sum_ctv * 1e4, 2)}bp²</td>
                <td>{pct(c.factor_variance / c.total_variance, 1)}</td>
              </tr>
              <tr>
                <td className="label">+ specific</td><td></td>
                <td>{signedNum(c.specific_variance * 1e4, 2)}bp²</td>
                <td>{pct(c.specific_variance / c.total_variance, 1)}</td>
              </tr>
              <tr style={{ fontWeight: 600 }}>
                <td className="label">√ total = model vol</td><td></td>
                <td>{pct(Math.sqrt(c.total_variance), 2)}</td>
                <td className="muted small">= {pct(c.vol_1d, 2)} hero</td>
              </tr>
            </tbody>
          </table>
          <p className="muted small">
            CTV_k = x_k·(Fx)_k — cross-terms split 50/50; a negative line hedges the book.
            Sums to factor variance; plus specific = total variance; √ recovers the
            {" "}{pct(c.vol_1d, 2)} book vol.
          </p>

          <h2>Positions — contribution to risk (CTR)</h2>
          <table className="tufte">
            <thead><tr><th className="label">Name</th><th>Weight</th><th>MCR</th><th>CTR</th>
              <th>% of vol</th></tr></thead>
            <tbody>
              {c.positions.slice(0, 20).map((r) => (
                <tr key={r.position}>
                  <td className="label">{r.ticker.toUpperCase()}</td>
                  <td>{pct(r.weight, 1)}</td>
                  <td>{num(r.mcr, 3)}</td>
                  <td>{pct(r.ctr, 3)}</td>
                  <td>{r.pct_of_vol === null ? "—" : pct(r.pct_of_vol, 1)}</td>
                </tr>
              ))}
              {c.positions.length > 20 && (
                <tr>
                  <td className="label muted">…{c.positions.length - 20} more</td><td></td><td></td>
                  <td>{pct(c.positions.slice(20).reduce((s, r) => s + r.ctr, 0), 3)}</td>
                  <td>{pct(c.positions.slice(20).reduce((s, r) => s + (r.pct_of_vol ?? 0), 0), 1)}</td>
                </tr>
              )}
              <tr style={{ fontWeight: 600 }}>
                <td className="label">Σ positions</td><td></td><td></td>
                <td>{pct(c.sum_ctr, 3)}</td>
                <td>{pct(c.sum_ctr / c.vol_1d, 0)}</td>
              </tr>
            </tbody>
          </table>
          <p className="muted small">
            MCR is a rate (risk per unit weight — nothing to sum); CTR = w·MCR sums exactly to
            book vol (Euler). CTR is in vol units, CTV in variance units — never compare the two
            directly. Model vol on the full factor-return history, distinct from the
            scenario-VaR views.
          </p>
        </div>
      )}
    </QueryState>
  );
}

// NB the old "Risk by level" tab (standalone Scenario VaR per Sector/Issuer/Position) was removed
// 2026-07-02 (itsjustbeta scope audit): ch 09 warns standalone per-bucket risk doesn't sum and
// names CTR the standard position-level report — the Euler tab supersedes it. The /attribution
// endpoint is parked, not removed.
export function Attribution() {
  const { date } = useApp();
  const [tab, setTab] = useState<"euler" | "pnl">("euler");

  return (
    <main className="lens">
      <h1>Attribution</h1>
      <p className="sub">
        {tab === "euler" ? `Euler risk contributions · as-of ${date}`
          : "Realized PnL by factor + residual · risk↔PnL reconcile"}
      </p>

      <div className="row" style={{ marginBottom: "0.8rem" }}>
        <button className={tab === "euler" ? "primary" : ""} onClick={() => setTab("euler")}>Contributions (Euler)</button>
        <button className={tab === "pnl" ? "primary" : ""} onClick={() => setTab("pnl")}>PnL attribution</button>
      </div>

      {tab === "euler" ? <EulerTab /> : <PnlTab />}
    </main>
  );
}
