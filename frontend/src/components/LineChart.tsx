// A small labelled line chart (shared by Trends small-multiples, Drift, Universe). Tufte: minimal
// chrome — a faint zero rule when the range straddles zero, the line, direct end-label, no legend.
// Hover shows the nearest point's x label + y value (TipBox).
import { useState } from "react";
import { LinePath, TipBox, svgPoint } from "./svg";

export interface Pt { x: number; y: number | null }

export function LineChart({
  title, points, labels, width = 240, height = 90, color = "#3b5e8c", fmt = (v) => v.toFixed(2),
  fill, zero,
}: {
  title?: string;
  points: Pt[];
  labels?: (string | number | undefined)[];   // indexed by each point's x (our callers use x = i)
  width?: number; height?: number; color?: string;
  fmt?: (v: number) => string;
  fill?: string;
  zero?: boolean;
}) {
  const [hov, setHov] = useState<number | null>(null);   // index into `clean`
  const clean = points.filter((p): p is { x: number; y: number } => p.y !== null && !Number.isNaN(p.y));
  if (clean.length < 2) {
    return (
      <div>
        {title && <div className="muted small">{title}</div>}
        <div className="muted small" style={{ height }}>insufficient data</div>
      </div>
    );
  }
  const ys = clean.map((p) => p.y);
  let yMin = Math.min(...ys), yMax = Math.max(...ys);
  if (zero) { yMin = Math.min(yMin, 0); yMax = Math.max(yMax, 0); }
  const pad = (yMax - yMin) * 0.08 || 0.01;
  yMin -= pad; yMax += pad;
  const last = clean[clean.length - 1].y;
  const first = clean[0].y;
  // mirror LinePath's scales for the hover hit-test
  const xs = clean.map((p) => p.x);
  const xmin = Math.min(...xs), xmax = Math.max(...xs);
  const xspan = xmax - xmin || 1;
  const sx = (x: number) => ((x - xmin) / xspan) * (width - 2) + 1;
  const sy = (y: number) => height - 1 - ((y - yMin) / (yMax - yMin)) * (height - 2);
  const nearest = (ux: number) => {
    let best = 0, bd = Infinity;
    clean.forEach((p, i) => {
      const dd = Math.abs(sx(p.x) - ux);
      if (dd < bd) { bd = dd; best = i; }
    });
    return best;
  };
  const hp = hov !== null ? clean[hov] : null;
  return (
    <div>
      {title && (
        <div className="row" style={{ justifyContent: "space-between" }}>
          <span className="muted small">{title}</span>
          <span className="num small">{fmt(last)}</span>
        </div>
      )}
      <svg width={width} height={height} role="img" aria-label={title}
        onMouseMove={(e) => { const p = svgPoint(e); if (p) setHov(nearest(p.x)); }}
        onMouseLeave={() => setHov(null)}>
        {zero && yMin < 0 && yMax > 0 && (
          <line x1={0} x2={width}
            y1={height - 1 - ((0 - yMin) / (yMax - yMin)) * (height - 2)}
            y2={height - 1 - ((0 - yMin) / (yMax - yMin)) * (height - 2)}
            stroke="#d8d5cd" strokeWidth={1} strokeDasharray="2 2" />
        )}
        <LinePath points={clean} width={width} height={height} color={color}
          fill={fill} yMin={yMin} yMax={yMax} />
        {hp && (
          <>
            <line x1={sx(hp.x)} x2={sx(hp.x)} y1={1} y2={height - 1}
              stroke="#c9c5bb" strokeWidth={0.8} strokeDasharray="2 2" pointerEvents="none" />
            <circle cx={sx(hp.x)} cy={sy(hp.y)} r={2.4} fill="#111" pointerEvents="none" />
            <TipBox x={sx(hp.x)} y={sy(hp.y)} width={width} height={height}
              lines={[...(labels?.[hp.x] !== undefined ? [String(labels[hp.x])] : []),
                      fmt(hp.y)]} />
          </>
        )}
      </svg>
      <div className="row small muted" style={{ justifyContent: "space-between" }}>
        <span className="num">{fmt(first)}</span>
      </div>
    </div>
  );
}
