// The view's `sort` (Streamlit colId form) applied tree-aware, and its round-trip; hide-empty.
import { describe, it, expect } from "vitest";
import { COL_SEP, LABEL_COL, TOTAL_COL, rowsFromRecords, sortIdFor, sortKeyFor, sortSiblings,
  type DisplayRow, type PivotConfig } from "./usePivot";

const cfg = { rows: ["Issuer", "Position"], cols: [], measures: ["Marginal Total VaR 99", "Net exposure"],
  filters: {}, totals: true, rowTot: false, hideEmpty: true, heat: false, asPct: true, prec: 3,
  sort: [], units: "weight", whatif: [], shocks: {} } as PivotConfig;

const row = (label: string, mtv: number | null, net = 0): DisplayRow => ({
  key: `/${label}`, label, level: 0, path: { Issuer: label }, expandable: false, expanded: false,
  values: { [`${COL_SEP}Marginal Total VaR 99`]: mtv, [`${COL_SEP}Net exposure`]: net },
});

describe("sort colId mapping", () => {
  it("maps Streamlit's measure / '(%)' / row-dim ids to grid keys and back", () => {
    expect(sortKeyFor("Marginal Total VaR 99", cfg)).toBe(`${COL_SEP}Marginal Total VaR 99`);
    expect(sortKeyFor("Marginal Total VaR 99 (%)", cfg)).toBe(`${COL_SEP}Marginal Total VaR 99`);
    expect(sortKeyFor("Issuer", cfg)).toBe(LABEL_COL);
    expect(sortKeyFor("nonsense", cfg)).toBeNull();
    expect(sortIdFor(`${COL_SEP}Net exposure`, cfg)).toBe("Net exposure");
    expect(sortIdFor(LABEL_COL, cfg)).toBe("Issuer");
  });
  it("maps a column-dim cell both ways", () => {
    const c = { ...cfg, cols: ["ScenarioSet"] };
    expect(sortKeyFor("HistFull / Net exposure", c)).toBe(`HistFull${COL_SEP}Net exposure`);
    expect(sortIdFor(`HistFull${COL_SEP}Net exposure`, c)).toBe("HistFull / Net exposure");
  });
});

describe("sortSiblings", () => {
  const rows = [row("b", 0.01), row("a", 0.03), row("c", null), row("d", 0.02)];
  it("orders by the saved sort, blanks last, and keeps cube order without a sort", () => {
    const desc = sortSiblings(rows, [{ colId: "Marginal Total VaR 99", sort: "desc" }], cfg);
    expect(desc.map((r) => r.label)).toEqual(["a", "d", "b", "c"]);
    const asc = sortSiblings(rows, [{ colId: "Marginal Total VaR 99", sort: "asc" }], cfg);
    expect(asc.map((r) => r.label)).toEqual(["b", "d", "a", "c"]);
    expect(sortSiblings(rows, [], cfg).map((r) => r.label)).toEqual(["b", "a", "c", "d"]);
  });
  it("sorts the label column by name", () => {
    expect(sortSiblings(rows, [{ colId: "Issuer", sort: "asc" }], cfg).map((r) => r.label))
      .toEqual(["a", "b", "c", "d"]);
  });
});

describe("rowsFromRecords Total column", () => {
  it("carries the cube's per_row margin under the Total col member, never a client sum", () => {
    const recs = [{ Sector: "Energy", ScenarioSet: "HistFull", "Net exposure": 0.1 },
                  { Sector: "Energy", ScenarioSet: "Evt:COVID2020", "Net exposure": 0.2 }];
    const per = [{ Sector: "Energy", "Net exposure": 0.7 }];   // whatever the cube says, verbatim
    const [r] = rowsFromRecords(recs, "Sector", "ScenarioSet", ["Net exposure"], 0, {}, "", false, per);
    expect(r.values[`${TOTAL_COL}${COL_SEP}Net exposure`]).toBe(0.7);
    expect(r.values[`HistFull${COL_SEP}Net exposure`]).toBe(0.1);
  });
});
