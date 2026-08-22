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
import type { PivotResult, Rec, SortItem } from "../api/types";

export const COL_SEP = "␟"; // separates colMember from measure in a value key
export const TOTAL_COL = "Total"; // the col member carrying the per-row cube margin (Total column)
export const LABEL_COL = "__label";

export interface PivotConfig {
  rows: string[];
  cols: string[];          // 0 or 1 col dim supported in the grid (layout transpose)
  measures: string[];
  filters: Record<string, string[]>;
  // Streamlit's names: col_tot = the Total ROW (∑ over rows: the cube's per_col / grand margin,
  // pinned bottom), row_tot = the Total COLUMN (∑ over columns: per_row margin; needs a col dim).
  totals: boolean;         // <- view `col_tot` (Total row)
  rowTot: boolean;         // <- view `row_tot` (Total column)
  hideEmpty: boolean;      // <- view `hide_empty`: drop all-blank body rows / columns, keep totals
  heat: boolean;
  asPct: boolean;
  prec: number;
  units: "weight" | "dollar";   // <- view `units`: dollar = weight-unit measures × Book MV (re-query)
  sort: SortItem[];        // <- view `sort`, Streamlit colIds (see sortKeyFor / sortIdFor)
  // hypothetical (transient cube branch/scenario per query; NOT persisted in saved views):
  whatif: { position: string; ticker: string; weight: number }[];
  shocks: Record<string, number>;
}

// the /pivot hypothetical query params for a config (shared by every query incl. drills)
export function hypoParams(cfg: PivotConfig): Record<string, string> {
  const p: Record<string, string> = {};
  if (cfg.whatif.length) {
    p.whatif = JSON.stringify(cfg.whatif.map(({ position, weight }) => ({ position, weight })));
  }
  if (Object.keys(cfg.shocks).length) p.shocks = JSON.stringify(cfg.shocks);
  return p;
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
  // the Total column is the cube's per_row margin at THIS level (levels = the row dims), so it is
  // requested with the level query itself — never summed client-side (VaR is non-additive)
  const wantRowTot = cfg.rowTot && !!colDim;
  return apiGet<PivotResult>("/pivot", {
    rows: wantRowTot ? levelDims.join(",") : levels.join(","),
    ...(wantRowTot ? { cols: colDim } : {}),
    measures: cfg.measures.join(","),
    filters: JSON.stringify(filters),
    totals: wantRowTot,
    ...(cfg.units === "dollar" ? { units: "dollar" } : {}),
    ...hypoParams(cfg),
  });
}

// ---- sort: Streamlit colId <-> grid value key -------------------------------------------------
// Streamlit saves a measure column as "<measure>" (or "<measure> (%)" under its % toggle), a
// column-dim cell as "<col member> / <measure>", and the label column as the row dim name.
export function sortKeyFor(id: string, cfg: PivotConfig): string | null {
  const norm = id.endsWith(" (%)") ? id.slice(0, -4) : id;
  if (norm === LABEL_COL || cfg.rows.includes(norm)) return LABEL_COL;
  if (cfg.measures.includes(norm)) return `${COL_SEP}${norm}`;
  const i = norm.indexOf(" / ");
  if (i > 0) {
    const a = norm.slice(0, i), b = norm.slice(i + 3);
    if (cfg.measures.includes(b)) return `${a}${COL_SEP}${b}`;
    if (cfg.measures.includes(a)) return `${b}${COL_SEP}${a}`;
  }
  if (norm.includes(COL_SEP)) return norm;     // already a grid key
  return null;
}
export function sortIdFor(key: string, cfg: PivotConfig): string {
  if (key === LABEL_COL) return cfg.rows[0] ?? LABEL_COL;
  const i = key.indexOf(COL_SEP);
  const cm = key.slice(0, i), m = key.slice(i + 1);
  return cm ? `${cm} / ${m}` : m;
}

// order siblings by the saved sort (nulls last, ties keep cube order); pure, unit-tested
export function sortSiblings(rows: DisplayRow[], sort: SortItem[], cfg: PivotConfig): DisplayRow[] {
  const items = [...sort].filter((s) => s.sort).sort((a, b) => (a.sortIndex ?? 0) - (b.sortIndex ?? 0));
  const keys = items.map((s) => ({ key: sortKeyFor(s.colId, cfg), dir: s.sort === "desc" ? -1 : 1 }))
    .filter((k): k is { key: string; dir: 1 | -1 } => !!k.key);
  if (!keys.length) return rows;
  const idx = new Map(rows.map((r, i) => [r.key, i]));
  return [...rows].sort((a, b) => {
    for (const { key, dir } of keys) {
      if (key === LABEL_COL) {
        const c = a.label.localeCompare(b.label, undefined, { numeric: true });
        if (c) return c * dir;
        continue;
      }
      const va = a.values[key], vb = b.values[key];
      const na = typeof va !== "number", nb = typeof vb !== "number";
      if (na && nb) continue;
      if (na) return 1;             // blanks last whichever direction
      if (nb) return -1;
      if (va !== vb) return (va - vb) * dir;
    }
    return idx.get(a.key)! - idx.get(b.key)!;
  });
}

const isBlank = (r: DisplayRow) => !Object.values(r.values).some((v) => typeof v === "number");

// Collapse the tidy records for one drill level into display rows keyed by the member of `dim`.
export function rowsFromRecords(records: Rec[], dim: string, colDim: string | undefined,
  measures: string[], level: number, parentPath: Record<string, string>,
  parentKey: string, expandable: boolean, perRow?: Rec[]): DisplayRow[] {
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
  // the Total column: the cube's per_row margin for each member (keyed on `dim`)
  for (const rec of perRow ?? []) {
    const row = byMember.get(String(rec[dim] ?? ""));
    if (!row) continue;
    for (const m of measures) {
      const v = rec[m];
      row.values[`${TOTAL_COL}${COL_SEP}${m}`] = typeof v === "number" ? v : null;
    }
  }
  return [...byMember.values()];
}

export function usePivot(initial: Partial<PivotConfig>) {
  const [cfg, setCfg] = useState<PivotConfig>({
    rows: ["Factor"], cols: [], measures: ["Net exposure"], filters: {},
    totals: true, rowTot: false, hideEmpty: true, heat: true, asPct: false, prec: 3, sort: [],
    units: "weight", whatif: [], shocks: {}, ...initial,
  });

  const [tree, setTree] = useState<Record<string, DisplayRow[]>>({}); // parentKey -> children
  const [topRows, setTopRows] = useState<DisplayRow[]>([]);
  const [colMembers, setColMembers] = useState<string[]>([""]);
  const [grand, setGrand] = useState<Record<string, number | null>>({});
  const [dollarMeasures, setDollarMeasures] = useState<string[]>([]);  // what /pivot priced in $
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
      if (colDim && c.rowTot) cms.push(TOTAL_COL);
      const expandable = c.rows.length > 1;
      const rows = rowsFromRecords(base.records, c.rows[0], colDim, c.measures, 0, {}, "", expandable,
        base.per_row);
      setColMembers(cms.length ? cms : [""]);
      setDollarMeasures(base.units === "dollar" ? (base.dollar_measures ?? []) : []);
      setTopRows(rows);
      setTree({});
      setWarning(base.warning);
      // the Total row: cube-computed margins only — the grand corner, plus per_col (one per col
      // member) when a column dim is on. Never a client-side sum.
      if (c.totals) {
        const g = await apiGet<PivotResult>("/pivot", {
          rows: c.rows[0], ...(colDim ? { cols: colDim } : {}), measures: c.measures.join(","),
          filters: JSON.stringify(c.filters), totals: true,
          ...(c.units === "dollar" ? { units: "dollar" } : {}),
          ...hypoParams(c),
        });
        const gr: Record<string, number | null> = {};
        for (const m of c.measures) gr[`${COL_SEP}${m}`] = g.grand?.[m] ?? null;
        if (colDim) {
          for (const rec of g.per_col ?? []) {
            const cm = String(rec[colDim] ?? "");
            for (const m of c.measures) {
              const v = rec[m];
              gr[`${cm}${COL_SEP}${m}`] = typeof v === "number" ? v : null;
            }
          }
          for (const m of c.measures) gr[`${TOTAL_COL}${COL_SEP}${m}`] = g.grand?.[m] ?? null;
        }
        setGrand(gr);
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
        row.level + 1, row.path, row.key, expandable, res.per_row);
      setTree((t) => ({ ...t, [row.key]: children }));
      const setExpanded = (rs: DisplayRow[]) => rs.map((r) => (r.key === row.key ? { ...r, expanded: true } : r));
      setTopRows((rs) => setExpanded(rs));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [cfg, tree]);

  // flatten the expanded tree into the ordered list AG Grid renders: siblings sorted by the
  // view's sort at EVERY level (the drill indentation survives a sort), all-blank body rows
  // dropped under hideEmpty (a parent with children showing is kept)
  const flat = useMemo(() => {
    const out: DisplayRow[] = [];
    const walk = (rows: DisplayRow[]) => {
      for (const r of sortSiblings(rows, cfg.sort, cfg)) {
        const expanded = !!tree[r.key];
        if (cfg.hideEmpty && isBlank(r) && !expanded) continue;
        out.push({ ...r, expanded });
        if (expanded) walk(tree[r.key]);
      }
    };
    walk(topRows);
    return out;
  }, [topRows, tree, cfg]);

  // hideEmpty also drops all-blank body COLUMNS (col members with no number in any shown row);
  // the Total column is kept, like Streamlit keeps total rows/cols
  const shownColMembers = useMemo(() => {
    if (!cfg.hideEmpty || colMembers.length <= 1) return colMembers;
    const kept = colMembers.filter((cm) => cm === TOTAL_COL || cm === ""
      || flat.some((r) => cfg.measures.some((m) => typeof r.values[`${cm}${COL_SEP}${m}`] === "number")));
    return kept.length ? kept : [""];
  }, [cfg.hideEmpty, cfg.measures, colMembers, flat]);

  return {
    cfg, setCfg, reload, toggleExpand,
    flat, colMembers: shownColMembers, grand, dollarMeasures, warning, loading, error,
  };
}
