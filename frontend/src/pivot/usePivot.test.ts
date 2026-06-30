import { describe, it, expect } from "vitest";
import { rowsFromRecords, mergeFilters, COL_SEP } from "./usePivot";
import type { Rec } from "../api/types";

describe("pivot drill helpers", () => {
  it("mergeFilters pins a parent member onto the base slicers", () => {
    const f = mergeFilters({ Date: ["2024-12-31"] }, { Factor: "Value" });
    expect(f).toEqual({ Date: ["2024-12-31"], Factor: ["Value"] });
  });

  it("rowsFromRecords keys rows by the member, sets level + indentation, marks expandable", () => {
    const records: Rec[] = [
      { Factor: "Value", "Net exposure": 0.5 },
      { Factor: "Momentum", "Net exposure": 0.2 },
    ];
    const rows = rowsFromRecords(records, "Factor", undefined, ["Net exposure"], 0, {}, "", true);
    expect(rows).toHaveLength(2);
    expect(rows[0].label).toBe("Value");
    expect(rows[0].level).toBe(0);
    expect(rows[0].expandable).toBe(true);
    expect(rows[0].values[`${COL_SEP}Net exposure`]).toBe(0.5);
    expect(rows[0].path).toEqual({ Factor: "Value" });
  });

  it("splices children one level deeper under the parent key (the drill)", () => {
    const children: Rec[] = [
      { Factor: "Value", Position: "FIGI1", "Net exposure": 0.3 },
      { Factor: "Value", Position: "FIGI2", "Net exposure": 0.2 },
    ];
    const rows = rowsFromRecords(children, "Position", undefined, ["Net exposure"], 1,
      { Factor: "Value" }, "/Value", false);
    expect(rows).toHaveLength(2);
    expect(rows[0].level).toBe(1);            // indented one level
    expect(rows[0].key.startsWith("/Value/")).toBe(true);
    expect(rows[0].path).toEqual({ Factor: "Value", Position: "FIGI1" });
    expect(rows[0].expandable).toBe(false);   // leaf
  });

  it("spreads a col dimension into per-(colMember,measure) value keys", () => {
    const records: Rec[] = [
      { Sector: "Tech", ScenarioSet: "HistFull", "Scenario VaR 99": 0.04 },
      { Sector: "Tech", ScenarioSet: "Evt:COVID2020", "Scenario VaR 99": 0.09 },
    ];
    const rows = rowsFromRecords(records, "Sector", "ScenarioSet", ["Scenario VaR 99"], 0, {}, "", false);
    expect(rows).toHaveLength(1);
    expect(rows[0].values[`HistFull${COL_SEP}Scenario VaR 99`]).toBe(0.04);
    expect(rows[0].values[`Evt:COVID2020${COL_SEP}Scenario VaR 99`]).toBe(0.09);
  });
});
