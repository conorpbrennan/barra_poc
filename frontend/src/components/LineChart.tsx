// A small labelled line chart (shared by Trends small-multiples, Drift, Universe). Tufte: minimal
// chrome — a faint zero rule when the range straddles zero, the line, direct end-label, no legend.
import { LinePath } from "./svg";

export interface Pt { x: number; y: number | null }

export function LineChart({
  title, points, width = 240, height = 90, color = "#3b5e8c", fmt = (v) => v.toFixed(2),
  fill, zero,
}: {
  title?: string;
  points: Pt[];
  width?: number; height?: number; color?: string;
  fmt?: (v: number) => string;
  fill?: string;
  zero?: boolean;
}) {
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
  return (
    <div>
      {title && (
        <div className="row" style={{ justifyContent: "space-between" }}>
          <span className="muted small">{title}</span>
          <span className="num small">{fmt(last)}</span>
        </div>
      )}
      <svg width={width} height={height} role="img" aria-label={title}>
        {zero && yMin < 0 && yMax > 0 && (
          <line x1={0} x2={width}
            y1={height - 1 - ((0 - yMin) / (yMax - yMin)) * (height - 2)}
            y2={height - 1 - ((0 - yMin) / (yMax - yMin)) * (height - 2)}
            stroke="#d8d5cd" strokeWidth={1} strokeDasharray="2 2" />
        )}
        <LinePath points={clean} width={width} height={height} color={color}
          fill={fill} yMin={yMin} yMax={yMax} />
      </svg>
      <div className="row small muted" style={{ justifyContent: "space-between" }}>
        <span className="num">{fmt(first)}</span>
      </div>
    </div>
  );
}
