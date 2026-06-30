// Chart mode (docs/vite-ui-plan.md §5), parity with render_spec: a view's chart is a COMPLETE
// Vega-Lite v5 spec whose data is ONE pivot query's records (the graph's "source" IS a /pivot query).
// react-vega renders the spec verbatim — we reuse saved specs, not reimplement charts. The builder
// reconstructs a spec from form controls (mark / x / measure / height) with a raw-JSON editor escape.
import { useEffect, useMemo, useState } from "react";
import { VegaLite } from "react-vega";
import { apiGet } from "../api/client";
import type { PivotResult, Rec } from "../api/types";
import type { PivotConfig } from "./usePivot";

type Spec = Record<string, unknown>;

function defaultSpec(rows: string[], measure: string, mark: string, height: number): Spec {
  return {
    $schema: "https://vega.github.io/schema/vega-lite/v5.json",
    mark: mark === "bar" ? { type: "bar", color: "#3b5e8c" } : { type: mark, color: "#3b5e8c", point: mark === "line" },
    width: "container",
    height,
    encoding: {
      x: { field: rows[0], type: "nominal", sort: "-y", axis: { labelAngle: -40 } },
      y: { field: measure, type: "quantitative", axis: { title: measure } },
      tooltip: [{ field: rows[0] }, { field: measure, type: "quantitative", format: ".4f" }],
    },
    config: { background: "#fffff8", view: { stroke: null }, axis: { grid: false, domainColor: "#d8d5cd", labelColor: "#111", titleColor: "#6b6b63" } },
    data: { name: "table" },
  };
}

export function ChartMode({ cfg, initialSpec }: { cfg: PivotConfig; initialSpec?: Spec | null }) {
  const [records, setRecords] = useState<Rec[]>([]);
  const [mark, setMark] = useState("bar");
  const [measure, setMeasure] = useState(cfg.measures[0] ?? "Net exposure");
  const [height, setHeight] = useState(280);
  const [raw, setRaw] = useState<string>("");
  const [useRaw, setUseRaw] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // run ONE query (the chart's source) — rows[0] on the axis, the measures as fields
  useEffect(() => {
    let live = true;
    apiGet<PivotResult>("/pivot", {
      rows: cfg.rows[0], measures: cfg.measures.join(","),
      filters: JSON.stringify(cfg.filters), totals: false,
    }).then((r) => { if (live) setRecords(r.records); }).catch((e) => setErr((e as Error).message));
    return () => { live = false; };
  }, [cfg.rows, cfg.measures, cfg.filters]);

  const spec = useMemo<Spec>(() => {
    if (useRaw && raw.trim()) {
      try { return JSON.parse(raw); } catch { return defaultSpec(cfg.rows, measure, mark, height); }
    }
    return initialSpec ?? defaultSpec(cfg.rows, measure, mark, height);
  }, [useRaw, raw, initialSpec, cfg.rows, measure, mark, height]);

  return (
    <div style={{ display: "flex", gap: "1.5rem", flexWrap: "wrap" }}>
      <div style={{ minWidth: "12rem" }}>
        <div className="muted small" style={{ marginBottom: "0.3rem" }}>Builder</div>
        <label className="row small" style={{ justifyContent: "space-between" }}>
          mark
          <select value={mark} onChange={(e) => setMark(e.target.value)}>
            {["bar", "line", "point", "area"].map((m) => <option key={m}>{m}</option>)}
          </select>
        </label>
        <label className="row small" style={{ justifyContent: "space-between", marginTop: "0.3rem" }}>
          measure
          <select value={measure} onChange={(e) => setMeasure(e.target.value)}>
            {cfg.measures.map((m) => <option key={m}>{m}</option>)}
          </select>
        </label>
        <label className="row small" style={{ justifyContent: "space-between", marginTop: "0.3rem" }}>
          height
          <input type="number" value={height} step={20} style={{ width: "4.5rem" }}
            onChange={(e) => setHeight(Number(e.target.value))} />
        </label>
        <label className="row small" style={{ marginTop: "0.5rem" }}>
          <input type="checkbox" checked={useRaw} onChange={(e) => setUseRaw(e.target.checked)} /> raw JSON spec
        </label>
        {useRaw && (
          <textarea value={raw} onChange={(e) => setRaw(e.target.value)} rows={12}
            placeholder="paste a Vega-Lite spec (data bound to the query records)"
            style={{ width: "16rem", fontFamily: "var(--mono)", fontSize: 11, marginTop: "0.3rem" }} />
        )}
      </div>
      <div style={{ flex: 1, minWidth: "20rem" }}>
        {err && <div className="err small">{err}</div>}
        <VegaLite spec={spec as never} data={{ table: records }} actions={false} />
        <div className="muted small">{records.length} rows · x = {cfg.rows[0]}</div>
      </div>
    </div>
  );
}
