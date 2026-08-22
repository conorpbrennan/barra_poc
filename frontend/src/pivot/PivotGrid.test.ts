// The grid's cell format must match the notebook's style_grid ({:.3%} -> "3.576%") and the
// Streamlit grid (toFixed(prec) on the percent) for the same `as_pct`/`prec` view state.
import { describe, it, expect } from "vitest";
import { fmt } from "./PivotGrid";
import type { PivotConfig } from "./usePivot";

const base = { rows: [], cols: [], measures: [], filters: {}, totals: false, heat: false,
  whatif: [], shocks: {} } as unknown as PivotConfig;

describe("PivotGrid fmt", () => {
  it("percent mode shows `prec` decimals of the percent (L1 Total VaR 99 = 3.576%)", () => {
    expect(fmt(0.03576148, { ...base, asPct: true, prec: 3 })).toBe("3.576%");
    expect(fmt(0.002626895, { ...base, asPct: true, prec: 3 })).toBe("0.263%");
  });
  it("fixed mode shows `prec` decimals", () => {
    expect(fmt(0.03576148, { ...base, asPct: false, prec: 3 })).toBe("0.036");
  });
  it("a dollar cell is whole dollars with separators, whatever prec/% say", () => {
    expect(fmt(151470121.4, { ...base, asPct: true, prec: 3 }, true)).toBe("$151,470,121");
    expect(fmt(-2500.6, { ...base, asPct: false, prec: 3 }, true)).toBe("-$2,501");
  });
  it("Market value / Book MV are money in every units mode", () => {
    // they are dollars by nature (never converted by units=dollar), so the money format must not
    // depend on the dollar_measures list — 4298665482 renders $4,298,665,482, not 4298665482.000
    expect(fmt(4298665482, { ...base, asPct: false, prec: 3 }, true)).toBe("$4,298,665,482");
  });
  it("non-numbers render empty", () => {
    expect(fmt(null, { ...base, asPct: true, prec: 3 })).toBe("");
  });
});
