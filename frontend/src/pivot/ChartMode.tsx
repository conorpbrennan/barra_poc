// Chart mode (docs/vite-ui-plan.md §5), parity with the Streamlit render_spec/_render_graphs:
// a charted view is SELF-DESCRIBING — `queries` (named, self-contained /pivot queries) + `chart`
// (a complete Vega-Lite v5 spec, or a LIST of them; each carries a `source` naming its query). We run
// each query and bind its records as that spec's default dataset, then render verbatim with react-vega
// — the charts are reused, not reimplemented. If a view has no saved chart (ad-hoc chart mode), the
// builder reconstructs a simple spec from form controls.
import { useEffect, useMemo, useRef, useState } from "react";
import { VegaLite } from "react-vega";
import { apiGet } from "../api/client";
import type { PivotResult, PivotQuery, Rec } from "../api/types";
import type { PivotConfig } from "./usePivot";

// A Vega-Lite spec is opaque JSON — we render saved specs verbatim, never introspect them.
export type VegaSpec = Record<string, unknown>;

// Measure the panel width and feed it to the spec as an explicit number. Vega-Lite's width:"container"
// measures 0 inside a flex:1/min-width:0 column (react-vega doesn't re-measure reliably there), which
// rendered the charts at zero width. A ResizeObserver-driven explicit width fixes it and stays responsive.
const FALLBACK_W = 900;   // used before layout / in jsdom (no layout); real width replaces it on mount
function useContainerWidth() {
  const ref = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => setW(Math.floor(el.getBoundingClientRect().width));
    measure();                                   // runs after layout -> real width immediately
    if (typeof ResizeObserver === "undefined") return;   // jsdom / non-browser: no live resize
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w] as const;
}
const specWidth = (w: number) => Math.max(320, (w || FALLBACK_W) - 4);

function runQuery(q: PivotQuery): Promise<Rec[]> {
  return apiGet<PivotResult>("/pivot", {
    rows: (q.rows ?? []).join(","), cols: (q.cols ?? []).join(","),
    measures: (q.measures ?? []).join(","),
    filters: JSON.stringify(q.filters ?? {}), totals: false,
  }).then((r) => r.records);
}

// ---- saved chart: run every query, bind records to each spec by its `source` name, render verbatim ----
function SavedChart({ queries, specs }: { queries: PivotQuery[]; specs: VegaSpec[] }) {
  const [dataByName, setDataByName] = useState<Record<string, Rec[]> | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [ref, width] = useContainerWidth();

  useEffect(() => {
    let live = true;
    setDataByName(null); setErr(null);
    Promise.all(queries.map(async (q) => [q.name, await runQuery(q)] as const))
      .then((pairs) => { if (live) setDataByName(Object.fromEntries(pairs)); })
      .catch((e) => { if (live) setErr((e as Error).message); });
    return () => { live = false; };
  }, [JSON.stringify(queries)]);

  return (
    <div ref={ref} style={{ width: "100%" }}>
      {err && <div className="err small">chart query rejected: {err}</div>}
      {!err && !dataByName && <div className="spin">rendering chart…</div>}
      {!err && dataByName && specs.map((raw, i) => {
        const sp: VegaSpec = JSON.parse(JSON.stringify(raw));         // deep copy before mutating
        // resolve this graph's query: by the spec's `source` NAME, else positional, else first.
        const source = (sp.source as string) ?? queries[i]?.name ?? queries[0]?.name;
        delete sp.source;
        const records = (source && dataByName[source]) || [];
        sp.data = { values: records };                                // bind as the default dataset
        sp.width = specWidth(width);                                  // explicit px — see useContainerWidth
        if (!records.length) {
          return <div key={i} className="muted small" style={{ margin: "0.6rem 0" }}>
            No data for “{source}” — needs a single Date + ScenarioSet slice.</div>;
        }
        return <div key={i} style={{ marginBottom: "1.2rem" }}>
          <VegaLite spec={sp as never} actions={false} />
        </div>;
      })}
    </div>
  );
}

// ---- ad-hoc builder (no saved chart): a simple spec from mark / measure / height ----
function defaultSpec(rows: string[], measure: string, mark: string, height: number, width: number): VegaSpec {
  return {
    $schema: "https://vega.github.io/schema/vega-lite/v5.json",
    mark: mark === "bar" ? { type: "bar", color: "#3b5e8c" } : { type: mark, color: "#3b5e8c", point: mark === "line" },
    width: specWidth(width), height,
    encoding: {
      x: { field: rows[0], type: "nominal", sort: "-y", axis: { labelAngle: -40 } },
      y: { field: measure, type: "quantitative", axis: { title: measure } },
      tooltip: [{ field: rows[0] }, { field: measure, type: "quantitative", format: ".4f" }],
    },
    config: { background: "#fffff8", view: { stroke: null }, axis: { grid: false, domainColor: "#d8d5cd", labelColor: "#111", titleColor: "#6b6b63" } },
    data: { name: "table" },
  };
}

function Builder({ cfg }: { cfg: PivotConfig }) {
  const [records, setRecords] = useState<Rec[]>([]);
  const [mark, setMark] = useState("bar");
  const [measure, setMeasure] = useState(cfg.measures[0] ?? "Net exposure");
  const [height, setHeight] = useState(280);
  const [raw, setRaw] = useState("");
  const [useRaw, setUseRaw] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [chartRef, chartW] = useContainerWidth();

  useEffect(() => {
    let live = true;
    runQuery({ name: "q", rows: cfg.rows.slice(0, 1), cols: [], measures: cfg.measures, filters: cfg.filters })
      .then((r) => { if (live) setRecords(r); }).catch((e) => setErr((e as Error).message));
    return () => { live = false; };
  }, [cfg.rows, cfg.measures, cfg.filters]);

  const spec = useMemo<VegaSpec>(() => {
    if (useRaw && raw.trim()) { try { return JSON.parse(raw); } catch { /* keep default */ } }
    return defaultSpec(cfg.rows, measure, mark, height, chartW);
  }, [useRaw, raw, cfg.rows, measure, mark, height, chartW]);

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
      <div ref={chartRef} style={{ flex: 1, minWidth: "20rem" }}>
        {err && <div className="err small">{err}</div>}
        <VegaLite spec={spec as never} data={{ table: records }} actions={false} />
        <div className="muted small">{records.length} rows · x = {cfg.rows[0]}</div>
      </div>
    </div>
  );
}

export function ChartMode({
  cfg, savedQueries, savedChart,
}: { cfg: PivotConfig; savedQueries?: PivotQuery[]; savedChart?: VegaSpec | VegaSpec[] | null }) {
  const specs = savedChart ? (Array.isArray(savedChart) ? savedChart : [savedChart]) : [];
  // a self-describing saved chart wins; otherwise fall back to the ad-hoc builder
  if (specs.length && savedQueries && savedQueries.length) {
    return <SavedChart queries={savedQueries} specs={specs} />;
  }
  return <Builder cfg={cfg} />;
}
