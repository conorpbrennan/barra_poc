// AG Grid Community as a PURE renderer (docs/vite-ui-plan.md §5): it holds only the rows on screen
// and never groups or sums. The first column draws the expand caret + indentation (the hand-rolled
// drill); measure columns get value formatters + an optional heatmap (ported from the st_aggrid
// JsCode formatters). The pinned bottom row is the cube's `grand` corner — the only total, never a
// client-side sum (VaR is non-additive).
import { useMemo } from "react";
import { AgGridReact } from "ag-grid-react";
import type { ColDef, ICellRendererParams } from "ag-grid-community";
import { COL_SEP, type DisplayRow, type PivotConfig } from "./usePivot";
import { pct, num } from "../lib/format";

interface GridRow {
  __row?: DisplayRow;
  __label: string;
  __total?: boolean;
  [key: string]: unknown;
}

function fmt(v: unknown, cfg: PivotConfig): string {
  if (typeof v !== "number" || Number.isNaN(v)) return "";
  return cfg.asPct ? pct(v, cfg.prec - 1) : num(v, cfg.prec);
}

// faint accent heatmap, scaled within a column's |range|
function heatStyle(v: number, min: number, max: number) {
  const lim = Math.max(Math.abs(min), Math.abs(max)) || 1;
  const t = Math.min(1, Math.abs(v) / lim);
  const alpha = (0.04 + 0.22 * t).toFixed(3);
  const rgb = v < 0 ? "163,50,43" : "59,94,140";
  return { backgroundColor: `rgba(${rgb},${alpha})` };
}

export function PivotGrid({
  flat, colMembers, measures, cfg, grand, onToggle,
}: {
  flat: DisplayRow[];
  colMembers: string[];
  measures: string[];
  cfg: PivotConfig;
  grand: Record<string, number | null>;
  onToggle: (r: DisplayRow) => void;
}) {
  const { rowData, columnDefs } = useMemo(() => {
    const cols = colMembers.length ? colMembers : [""];

    // per-value-column min/max for the heatmap
    const ranges = new Map<string, { min: number; max: number }>();
    for (const cm of cols) for (const m of measures) {
      const key = `${cm}${COL_SEP}${m}`;
      let min = Infinity, max = -Infinity;
      for (const r of flat) {
        const v = r.values[key];
        if (typeof v === "number") { min = Math.min(min, v); max = Math.max(max, v); }
      }
      ranges.set(key, { min, max });
    }

    const rows: GridRow[] = flat.map((r) => {
      const gr: GridRow = { __row: r, __label: r.label };
      for (const cm of cols) for (const m of measures) {
        const key = `${cm}${COL_SEP}${m}`;
        gr[key] = r.values[key] ?? null;
      }
      return gr;
    });

    const labelCol: ColDef<GridRow> = {
      headerName: cfg.rows.join(" › "),
      field: "__label",
      pinned: "left",
      width: 230,
      cellRenderer: (p: ICellRendererParams<GridRow>) => {
        const r = p.data?.__row;
        const indent = (r?.level ?? 0) * 14;
        const caret = r?.expandable ? (r.expanded ? "▾" : "▸") : "";
        return (
          <span style={{ paddingLeft: indent, cursor: r?.expandable ? "pointer" : "default",
            fontWeight: p.data?.__total ? 600 : 400 }}
            onClick={() => r?.expandable && onToggle(r)}>
            <span style={{ display: "inline-block", width: 14, color: "var(--accent)" }}>{caret}</span>
            {p.value as string}
          </span>
        );
      },
    };

    const valueCols: ColDef<GridRow>[] = [];
    for (const cm of cols) {
      for (const m of measures) {
        const key = `${cm}${COL_SEP}${m}`;
        valueCols.push({
          headerName: cm ? `${cm} · ${m}` : m,
          field: key,
          type: "rightAligned",
          width: 140,
          valueFormatter: (p) => fmt(p.value, cfg),
          cellStyle: (p) => {
            if (!cfg.heat || typeof p.value !== "number") return { fontVariantNumeric: "tabular-nums" };
            const rg = ranges.get(key)!;
            return { fontVariantNumeric: "tabular-nums", ...heatStyle(p.value, rg.min, rg.max) };
          },
        });
      }
    }

    return { rowData: rows, columnDefs: [labelCol, ...valueCols] };
  }, [flat, colMembers, measures, cfg, onToggle]);

  const pinnedBottomRowData = useMemo(() => {
    if (!cfg.totals || !Object.keys(grand).length) return [];
    const cols = colMembers.length ? colMembers : [""];
    const tr: GridRow = { __label: "Total (book)", __total: true };
    for (const m of measures) tr[`${cols[0]}${COL_SEP}${m}`] = grand[m] ?? null;
    return [tr];
  }, [cfg.totals, grand, colMembers, measures]);

  return (
    <div className="ag-theme-balham" style={{ height: "70vh", width: "100%" }}>
      <AgGridReact<GridRow>
        rowData={rowData}
        columnDefs={columnDefs}
        pinnedBottomRowData={pinnedBottomRowData}
        defaultColDef={{ sortable: true, resizable: true }}
        suppressCellFocus
        headerHeight={30}
        rowHeight={26}
        animateRows={false}
      />
    </div>
  );
}
