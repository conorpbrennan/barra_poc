// Pivot workspace state + the hand-rolled server-side drill (docs/vite-ui-plan.md §5).
//
// INVARIANT: the grid is a pure renderer. Every reshape and every drill is a /pivot call to Atoti
// behind the _validate_pivot allowlist; the browser never groups, sums, or pivots numbers itself.
// VaR is non-additive, so the only total we render is the cube's `grand` corner — never a client sum.
//
// The drill is lazy: the base query asks rows=[rowDims[0]] (+ an optional single col dim) and shows
// one row per top member. Expanding a row issues a fresh /pivot for the NEXT row dim, filtered to the
// parent's member path, and splices the returned children underneath (indented). Collapsing drops them.
import { useCallback, useMemo, useState } from "react";
import { apiGet } from "../api/client";
import type { PivotResult, Rec } from "../api/types";

export const COL_SEP = "␟"; // separates colMember from measure in a value key

export interface PivotConfig {
  rows: string[];
  cols: string[];          // 0 or 1 col dim supported in the grid (layout transpose)
  measures: string[];
  filters: Record<string, string[]>;
  totals: boolean;
  heat: boolean;
  asPct: boolean;
  prec: number;
}

export interface DisplayRow {
  key: string;
  label: string;
  level: number;
  path: Record<string, string>;
  values: Record<string, number | null>;
  expandable: boolean;
  expanded: boolean;
}

export function mergeFilters(base: Record<string, string[]>, path: Record<string, string>) {
  const f = { ...base };
  for (const [k, v] of Object.entries(path)) f[k] = [v];
  return f;
}

async function queryLevel(
  cfg: PivotConfig, levelDims: string[], path: Record<string, string>,
): Promise<PivotResult> {
  const colDim = cfg.cols[0];
  const levels = colDim ? [...levelDims, colDim] : levelDims;
  const filters = mergeFilters(cfg.filters, path);
  return apiGet<PivotResult>("/pivot", {
    rows: levels.join(","),
    measures: cfg.measures.join(","),
    filters: JSON.stringify(filters),
    totals: false,
  });
}

// Collapse the tidy records for one drill level into display rows keyed by the member of `dim`.
export function rowsFromRecords(records: Rec[], dim: string, colDim: string | undefined,
  measures: string[], level: number, parentPath: Record<string, string>,
  parentKey: string, expandable: boolean): DisplayRow[] {
  const byMember = new Map<string, DisplayRow>();
  for (const rec of records) {
    const member = String(rec[dim] ?? "");
    if (member === "") continue;
    const key = `${parentKey}/${member}`;
    let row = byMember.get(member);
    if (!row) {
      row = {
        key, label: member, level,
        path: { ...parentPath, [dim]: member },
        values: {}, expandable, expanded: false,
      };
      byMember.set(member, row);
    }
    const colMember = colDim ? String(rec[colDim] ?? "") : "";
    for (const m of measures) {
      const v = rec[m];
      row.values[`${colMember}${COL_SEP}${m}`] = typeof v === "number" ? v : null;
    }
  }
  return [...byMember.values()];
}

export function usePivot(initial: Partial<PivotConfig>) {
  const [cfg, setCfg] = useState<PivotConfig>({
    rows: ["Factor"], cols: [], measures: ["Net exposure"], filters: {},
    totals: true, heat: true, asPct: false, prec: 3, ...initial,
  });

  const [tree, setTree] = useState<Record<string, DisplayRow[]>>({}); // parentKey -> children
  const [topRows, setTopRows] = useState<DisplayRow[]>([]);
  const [colMembers, setColMembers] = useState<string[]>([""]);
  const [grand, setGrand] = useState<Record<string, number | null>>({});
  const [warning, setWarning] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // (re)load the base level + reset the tree. Called on Apply.
  const reload = useCallback(async (override?: PivotConfig) => {
    const c = override ?? cfg;
    if (!c.rows.length || !c.measures.length) {
      setError("pick at least one row field and one measure");
      return;
    }
    setLoading(true); setError(null);
    try {
      const base = await queryLevel(c, [c.rows[0]], {});
      const colDim = c.cols[0];
      const cms = colDim
        ? Array.from(new Set(base.records.map((r) => String(r[colDim] ?? "")))).filter(Boolean).sort()
        : [""];
      const expandable = c.rows.length > 1;
      const rows = rowsFromRecords(base.records, c.rows[0], colDim, c.measures, 0, {}, "", expandable);
      setColMembers(cms.length ? cms : [""]);
      setTopRows(rows);
      setTree({});
      setWarning(base.warning);
      // grand corner (always cube-computed; the only total we render)
      if (c.totals) {
        const g = await apiGet<PivotResult>("/pivot", {
          rows: c.rows[0], measures: c.measures.join(","),
          filters: JSON.stringify(c.filters), totals: true,
        });
        setGrand(g.grand ?? {});
      } else setGrand({});
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [cfg]);

  const toggleExpand = useCallback(async (row: DisplayRow) => {
    if (row.level >= cfg.rows.length - 1) return;
    if (tree[row.key]) {
      // collapse: drop children (and any deeper cached descendants stay cached but hidden)
      const flip = (rs: DisplayRow[]) => rs.map((r) => (r.key === row.key ? { ...r, expanded: false } : r));
      setTopRows((rs) => flip(rs));
      setTree((t) => {
        const nt = { ...t };
        // mark collapsed by removing the entry so flatten hides children
        delete nt[row.key];
        return nt;
      });
      return;
    }
    setLoading(true);
    try {
      const nextDim = cfg.rows[row.level + 1];
      const res = await queryLevel(cfg, cfg.rows.slice(0, row.level + 2), row.path);
      const expandable = row.level + 1 < cfg.rows.length - 1;
      const children = rowsFromRecords(res.records, nextDim, cfg.cols[0], cfg.measures,
        row.level + 1, row.path, row.key, expandable);
      setTree((t) => ({ ...t, [row.key]: children }));
      const setExpanded = (rs: DisplayRow[]) => rs.map((r) => (r.key === row.key ? { ...r, expanded: true } : r));
      setTopRows((rs) => setExpanded(rs));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [cfg, tree]);

  // flatten the expanded tree into the ordered list AG Grid renders
  const flat = useMemo(() => {
    const out: DisplayRow[] = [];
    const walk = (rows: DisplayRow[]) => {
      for (const r of rows) {
        const expanded = !!tree[r.key];
        out.push({ ...r, expanded });
        if (expanded) walk(tree[r.key]);
      }
    };
    walk(topRows);
    return out;
  }, [topRows, tree]);

  return {
    cfg, setCfg, reload, toggleExpand,
    flat, colMembers, grand, warning, loading, error,
  };
}
