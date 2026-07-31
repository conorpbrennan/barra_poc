// Hand-rolled SVG primitives (no chart lib) — sparkline, bullet graph, direct-labelled bar,
// plus the shared hover-tooltip helpers (svgPoint / TipBox) every chart uses.
// Few/Tufte idioms: high data-ink, grey + one accent, status colour only where it carries meaning.
import { useState } from "react";

const ACCENT = "#3b5e8c";
const INK = "#111";
const FAINT = "#6b6b63";
const LINE = "#d8d5cd";

// ---- hover helpers ----
// Mouse event -> SVG user-space coordinates (correct under viewBox scaling too).
export function svgPoint(e: React.MouseEvent<SVGSVGElement>): { x: number; y: number } | null {
  const svg = e.currentTarget;
  const ctm = svg.getScreenCTM();
  if (!ctm) return null;
  const pt = new DOMPoint(e.clientX, e.clientY).matrixTransform(ctm.inverse());
  return { x: pt.x, y: pt.y };
}

// Small tooltip box drawn INSIDE the svg (no portal), clamped to the frame.
export function TipBox({ x, y, lines, width, height }: {
  x: number; y: number; lines: string[]; width: number; height: number;
}) {
  const w = Math.max(...lines.map((l) => l.length)) * 6.1 + 12;
  const h = lines.length * 13 + 7;
  const tx = Math.min(Math.max(x + 9, 2), Math.max(2, width - w - 2));
  const ty = Math.min(Math.max(2, y - h - 8), Math.max(2, height - h - 2));
  return (
    <g pointerEvents="none">
      <rect x={tx} y={ty} width={w} height={h} rx={3}
        fill="#fffff8" stroke="#c9c5bb" strokeWidth={0.8} opacity={0.97} />
      {lines.map((l, i) => (
        <text key={i} x={tx + 6} y={ty + 12 + i * 13} fontSize={10.5} fill={INK}
          className="num">{l}</text>
      ))}
    </g>
  );
}

// ---- Sparkline: inline trend, no axes. Last point dotted. Hover -> nearest x,y. ----
export function Sparkline({
  values, labels, width = 86, height = 22, color = ACCENT, baseline, fmt,
}: {
  values: (number | null)[]; labels?: (string | undefined)[];
  width?: number; height?: number; color?: string; baseline?: number;
  fmt?: (v: number) => string;
}) {
  const [hov, setHov] = useState<number | null>(null);
  const v = values.filter((x): x is number => x !== null && !Number.isNaN(x));
  if (v.length < 2) return <svg width={width} height={height} aria-hidden />;
  const min = Math.min(...v, baseline ?? Infinity);
  const max = Math.max(...v, baseline ?? -Infinity);
  const span = max - min || 1;
  const n = values.length;
  const x = (i: number) => (i / (n - 1)) * (width - 2) + 1;
  const y = (val: number) => height - 1 - ((val - min) / span) * (height - 2);
  let d = "";
  let started = false;
  let lastX = 0, lastY = 0;
  values.forEach((val, i) => {
    if (val === null || Number.isNaN(val)) return;
    const px = x(i), py = y(val);
    d += `${started ? "L" : "M"}${px.toFixed(1)},${py.toFixed(1)}`;
    started = true; lastX = px; lastY = py;
  });
  const nearest = (ux: number) => {
    let best: number | null = null, bd = Infinity;
    values.forEach((val, i) => {
      if (val === null || Number.isNaN(val)) return;
      const dd = Math.abs(x(i) - ux);
      if (dd < bd) { bd = dd; best = i; }
    });
    return best;
  };
  const hv = hov !== null ? values[hov] : null;
  return (
    <svg width={width} height={height} role="img" aria-label="trend"
      style={{ overflow: "visible" }}
      onMouseMove={(e) => { const p = svgPoint(e); if (p) setHov(nearest(p.x)); }}
      onMouseLeave={() => setHov(null)}>
      {baseline !== undefined && (
        <line x1={1} x2={width - 1} y1={y(baseline)} y2={y(baseline)}
          stroke={LINE} strokeWidth={1} strokeDasharray="2 2" />
      )}
      <path d={d} fill="none" stroke={color} strokeWidth={1.25} />
      <circle cx={lastX} cy={lastY} r={1.7} fill={color} />
      {hov !== null && hv !== null && hv !== undefined && (
        <>
          <circle cx={x(hov)} cy={y(hv)} r={2.2} fill={INK} />
          <TipBox x={x(hov)} y={y(hv)} width={width} height={height}
            lines={[...(labels?.[hov] ? [String(labels[hov])] : []),
                    fmt ? fmt(hv) : hv.toPrecision(3)]} />
        </>
      )}
    </svg>
  );
}

// ---- Bullet graph (Few): a measure (the bar) against warn/limit bands, no gauge chrome. ----
export function BulletGraph({
  value, warn, limit, max, width = 150, height = 16, status,
}: {
  value: number | null; warn: number | null; limit: number | null;
  max?: number; width?: number; height?: number; status?: string;
}) {
  if (value === null) return <span className="muted small">—</span>;
  const top = max ?? (Math.max(value, limit ?? 0, warn ?? 0) * 1.25 || 1);
  const w = (v: number) => Math.max(0, Math.min(1, v / top)) * width;
  const barColor = status === "breach" ? "#a3322b" : status === "amber" ? "#b5651d" : INK;
  const h2 = height / 2;
  const tip = `value ${(value * 100).toFixed(2)}%`
    + (warn !== null ? ` · warn ${(warn * 100).toFixed(1)}%` : "")
    + (limit !== null ? ` · limit ${(limit * 100).toFixed(1)}%` : "");
  return (
    <svg width={width} height={height} role="img" aria-label="bullet">
      <title>{tip}</title>
      {/* qualitative bands: ok (lightest) -> warn -> over-limit */}
      <rect x={0} y={0} width={width} height={height} fill="#f1efe9" />
      {warn !== null && <rect x={0} y={0} width={w(warn)} height={height} fill="#e8e6dd" />}
      {/* the measure bar (centred, thinner — Few) */}
      <rect x={0} y={h2 - 3} width={w(value)} height={6} fill={barColor} />
      {/* limit marker */}
      {limit !== null && (
        <line x1={w(limit)} x2={w(limit)} y1={1} y2={height - 1} stroke="#a3322b" strokeWidth={1.5} />
      )}
      {warn !== null && (
        <line x1={w(warn)} x2={w(warn)} y1={2} y2={height - 2} stroke="#b5651d" strokeWidth={1} />
      )}
    </svg>
  );
}

// ---- Direct-labelled horizontal bar (for top exposures, contributions). ----
export function LabelBar({
  label, value, max, width = 150, suffix = "", color = ACCENT, neg,
}: {
  label: string; value: number; max: number; width?: number; suffix?: string;
  color?: string; neg?: boolean;
}) {
  const frac = max > 0 ? Math.min(1, Math.abs(value) / max) : 0;
  const barColor = neg && value < 0 ? "#a3322b" : color;
  return (
    <div className="row" style={{ gap: "0.6rem" }}>
      <span style={{ width: "7rem", textAlign: "right", color: FAINT, fontSize: 12.5 }}>{label}</span>
      <svg width={width} height={12} role="img" aria-label={`${label} ${value}`}>
        <title>{`${label}: ${value.toFixed(2)}${suffix}`}</title>
        <rect x={0} y={3} width={frac * width} height={6} fill={barColor} />
      </svg>
      <span className="num" style={{ fontSize: 12.5, minWidth: "3.4rem" }}>
        {value.toFixed(2)}{suffix}
      </span>
    </div>
  );
}

// ---- Tiny line+area path helper for equity/underwater curves ----
export function LinePath({
  points, width, height, color = ACCENT, fill, yMin, yMax,
}: {
  points: { x: number; y: number }[]; width: number; height: number;
  color?: string; fill?: string; yMin?: number; yMax?: number;
}) {
  if (points.length < 2) return null;
  const xs = points.map((p) => p.x), ys = points.map((p) => p.y);
  const xmin = Math.min(...xs), xmax = Math.max(...xs);
  const ymin = yMin ?? Math.min(...ys), ymax = yMax ?? Math.max(...ys);
  const xspan = xmax - xmin || 1, yspan = ymax - ymin || 1;
  const sx = (x: number) => ((x - xmin) / xspan) * (width - 2) + 1;
  const sy = (y: number) => height - 1 - ((y - ymin) / yspan) * (height - 2);
  const d = points.map((p, i) => `${i ? "L" : "M"}${sx(p.x).toFixed(1)},${sy(p.y).toFixed(1)}`).join("");
  const baseY = sy(yMin !== undefined && yMin <= 0 && ymax >= 0 ? 0 : ymin);
  const area = fill
    ? `${d}L${sx(xmax).toFixed(1)},${baseY.toFixed(1)}L${sx(xmin).toFixed(1)},${baseY.toFixed(1)}Z`
    : "";
  return (
    <>
      {fill && <path d={area} fill={fill} stroke="none" />}
      <path d={d} fill="none" stroke={color} strokeWidth={1.25} />
    </>
  );
}
