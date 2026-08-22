// AG Grid Community as a PURE renderer (docs/vite-ui-plan.md §5): it holds only the rows on screen
// and never groups or sums. The first column draws the expand caret + indentation (the hand-rolled
// drill); measure columns get value formatters + an optional heatmap (ported from the st_aggrid
// JsCode formatters). The pinned bottom row is the cube's `grand` corner — the only total, never a
// client-side sum (VaR is non-additive).
import { useCallback, useMemo } from "react";
import { AgGridReact } from "ag-grid-react";
import type { ColDef, GridReadyEvent, ICellRendererParams, SortChangedEvent } from "ag-grid-community";
import { COL_SEP, LABEL_COL, TOTAL_COL, sortIdFor, sortKeyFor, type DisplayRow, type PivotConfig } from "./usePivot";
import type { SortItem } from "../api/types";
import { pct, num, money } from "../lib/format";

interface GridRow {
  __row?: DisplayRow;
  __label: string;
  __total?: boolean;
  __ord: number;           // position in the tree-aware sorted `flat` — what every column sorts on
  [key: string]: unknown;
}

// Sorting happens in usePivot (siblings within each drill level, by the view's `sort`), so the
// grid's own sort — whichever column, whichever direction — must just reproduce `flat`'s order:
// the comparator compares tree positions and lets AG Grid flip the sign for "desc". The header
// arrow then shows the active sort while the drill indentation stays intact.
const treeOrder = (_a: unknown, _b: unknown, na: { data?: GridRow }, nb: { data?: GridRow }, desc: boolean) =>
  ((na.data?.__ord ?? 0) - (nb.data?.__ord ?? 0)) * (desc ? -1 : 1);

export function fmt(v: unknown, cfg: PivotConfig, dollar = false): string {
  if (typeof v !== "number" || Number.isNaN(v)) return "";
  if (dollar) return money(v);   // a "$" cell: whole dollars, whatever prec/% say
  // `prec` is decimals of the DISPLAYED number in both modes, as the notebook's style_grid
  // ({:.3%} -> 3.576%) and the Streamlit grid do — the old `prec - 1` showed 3.58% against
  // the notebook's 3.576% for the same cell.
  return cfg.asPct ? pct(v, cfg.prec) : num(v, cfg.prec);
}

// Measures that ARE dollars regardless of the Units toggle (not converted by units=dollar, so
// never in `dollar_measures`): always money-formatted.
const ALWAYS_DOLLAR = new Set(["Market value", "Manager MV"]);

// faint accent heatmap, scaled within a column's |range|
function heatStyle(v: number, min: number, max: number) {
  const lim = Math.max(Math.abs(min), Math.abs(max)) || 1;
  const t = Math.min(1, Math.abs(v) / lim);
  const alpha = (0.04 + 0.22 * t).toFixed(3);
  const rgb = v < 0 ? "163,50,43" : "59,94,140";
  return { backgroundColor: `rgba(${rgb},${alpha})` };
}

export function PivotGrid({
  flat, colMembers, measures, cfg, grand, dollarMeasures = [], onToggle, onSort,
}: {
  flat: DisplayRow[];
  colMembers: string[];
  measures: string[];
  cfg: PivotConfig;
  grand: Record<string, number | null>;
  dollarMeasures?: string[];
  onToggle: (r: DisplayRow) => void;
  onSort?: (sort: SortItem[]) => void;
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

    const rows: GridRow[] = flat.map((r, i) => {
      const gr: GridRow = { __row: r, __label: r.label, __ord: i };
      for (const cm of cols) for (const m of measures) {
        const key = `${cm}${COL_SEP}${m}`;
        gr[key] = r.values[key] ?? null;
      }
      return gr;
    });

    const labelCol: ColDef<GridRow> = {
      headerName: cfg.rows.join(" › "),
      field: LABEL_COL,
      colId: LABEL_COL,
      comparator: treeOrder,
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
          comparator: treeOrder,
          // measure names contain dots (e.g. "Scenario VaR 97.5"); AG Grid reads a dotted `field` as a
          // nested path and renders blank, so read the literal key via valueGetter (colId keeps identity).
          colId: key,
          valueGetter: (p) => (p.data ? (p.data as GridRow)[key] as number | null : null),
          type: "rightAligned",
          width: 140,
          valueFormatter: (p) => fmt(p.value, cfg, dollarMeasures.includes(m) || ALWAYS_DOLLAR.has(m)),
          cellStyle: (p) => {
            if (!cfg.heat || typeof p.value !== "number") return { fontVariantNumeric: "tabular-nums" };
            const rg = ranges.get(key)!;
            return { fontVariantNumeric: "tabular-nums", ...heatStyle(p.value, rg.min, rg.max) };
          },
        });
      }
    }

    return { rowData: rows, columnDefs: [labelCol, ...valueCols] };
  }, [flat, colMembers, measures, cfg, dollarMeasures, onToggle]);

  const pinnedBottomRowData = useMemo(() => {
    if (!cfg.totals || !Object.keys(grand).length) return [];
    const cols = colMembers.length ? colMembers : [""];
    const tr: GridRow = { __label: "Total (manager)", __total: true, __ord: -1 };
    for (const cm of cols) for (const m of measures) {
      const key = `${cm}${COL_SEP}${m}`;
      tr[key] = grand[key] ?? (cm === TOTAL_COL ? grand[`${COL_SEP}${m}`] : null) ?? null;
    }
    return [tr];
  }, [cfg.totals, grand, colMembers, measures]);

  // the view's saved sort -> grid column state (applied on mount; the grid remounts per view)
  const applySort = useCallback((e: GridReadyEvent<GridRow>) => {
    const state = cfg.sort.map((s, i) => {
      const key = sortKeyFor(s.colId, cfg);
      return key ? { colId: key, sort: s.sort, sortIndex: s.sortIndex ?? i } : null;
    }).filter((x): x is { colId: string; sort: "asc" | "desc"; sortIndex: number } => !!x);
    e.api.applyColumnState({ state, defaultState: { sort: null } });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cfg.sort, cfg.rows, cfg.measures]);
  // a header click -> the view's sort, in Streamlit's colId form, so a save round-trips
  const captureSort = useCallback((e: SortChangedEvent<GridRow>) => {
    const next: SortItem[] = e.api.getColumnState()
      .filter((c) => c.sort)
      .sort((a, b) => (a.sortIndex ?? 0) - (b.sortIndex ?? 0))
      .map((c, i) => ({ colId: sortIdFor(c.colId, cfg), sort: c.sort as "asc" | "desc", sortIndex: i }));
    if (JSON.stringify(next) !== JSON.stringify(cfg.sort)) onSort?.(next);
  }, [cfg, onSort]);

  // Remount the grid when the COLUMN structure changes (loading a different view: new rows/measures/
  // col members). AG Grid can otherwise keep stale columns when both columnDefs and rowData swap at
  // once — the "first click shows the old report, second click is right" symptom. The key excludes the
  // expanded/drill state (flat), so drilling within a view does NOT remount and keeps scroll/expansion.
  const gridKey = `${cfg.rows.join(",")}|${measures.join(",")}|${colMembers.join(",")}`;

  // Size the grid to its rows (capped at 70vh) so the pinned Total row sits directly under the
  // last data row instead of at the foot of an empty viewport.
  const ROW = 26, HEADER = 30;
  const height = `min(70vh, ${HEADER + ROW * (rowData.length + pinnedBottomRowData.length) + 18}px)`;

  return (
    <div className="ag-theme-balham" style={{ height, width: "100%" }}>
      <AgGridReact<GridRow>
        key={gridKey}
        rowData={rowData}
        columnDefs={columnDefs}
        pinnedBottomRowData={pinnedBottomRowData}
        onGridReady={applySort}
        onSortChanged={captureSort}
        defaultColDef={{ sortable: true, resizable: true }}
        suppressCellFocus
        headerHeight={30}
        rowHeight={26}
        animateRows={false}
      />
    </div>
  );
}
