// Verifies chart mode renders a SELF-DESCRIBING saved view: it runs each named query and binds that
// query's records into the matching spec (by `source`). Reproduces the "Scenario P&L — COVID 2020"
// bug where the saved queries/chart were ignored and nothing rendered. react-vega is mocked to a probe
// that reports how many data rows each spec received, and fetch is stubbed per query.
import { render, screen, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";

// probe: show the bound row count + whether a `source` leaked through (it must be stripped)
vi.mock("react-vega", () => ({
  VegaLite: ({ spec }: { spec: { data?: { values?: unknown[] }; source?: string } }) => (
    <div data-testid="vega" data-rows={spec.data?.values?.length ?? 0} data-hassource={String("source" in spec)} />
  ),
}));

import { ChartMode, type VegaSpec } from "./ChartMode";
import type { PivotQuery } from "../api/types";

// The fast per-day path (Day/DayDate levels + PnL at day, sliced by DaySet) — the shape the saved
// COVID chart views author since 2026-08-15; the legacy ScenarioDay path is not used here.
const QUERIES: PivotQuery[] = [
  { name: "Scenario P&L", rows: ["Day", "DayDate"], cols: [], measures: ["PnL at day"],
    filters: { Book: ["Soros"], ScenarioSet: ["Evt:COVID2020"], DaySet: ["Evt:COVID2020"] } },
  { name: "Scenario P&L by Sector", rows: ["Day", "DayDate", "Sector"], cols: [], measures: ["PnL at day"],
    filters: { Book: ["Soros"], ScenarioSet: ["Evt:COVID2020"], DaySet: ["Evt:COVID2020"] } },
];
const CHART: VegaSpec[] = [
  { source: "Scenario P&L", mark: "line", data: { name: "x" } },
  { source: "Scenario P&L by Sector", mark: "area", data: { name: "x" } },
];

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const u = new URL(url, "http://x");
    const rows = u.searchParams.get("rows") || "";
    // path query -> 3 day rows; sector breakout -> 6 rows. Distinguishable by the rows param.
    const n = rows.includes("Sector") ? 6 : 3;
    const records = Array.from({ length: n }, (_, i) => ({ Day: i, DayDate: `2020-03-0${i + 1}`, "PnL at day": i * 0.01 }));
    return { ok: true, status: 200, json: async () => ({ records }), text: async () => "" };
  }) as unknown as typeof fetch);
});

const cfg = { rows: ["Factor"], cols: [], measures: ["Scenario VaR 99"], filters: {},
  totals: false, rowTot: false, hideEmpty: true, heat: true, asPct: false, prec: 3, sort: [], whatif: [], shocks: {} };

describe("ChartMode — saved chart", () => {
  it("renders each saved spec with its query's records bound (source resolved + stripped)", async () => {
    render(<ChartMode cfg={cfg} savedQueries={QUERIES} savedChart={CHART} />);
    await waitFor(() => expect(screen.getAllByTestId("vega")).toHaveLength(2));
    const charts = screen.getAllByTestId("vega");
    // spec 1 <- "Scenario P&L" (3 rows); spec 2 <- "...by Sector" (6 rows)
    expect(charts[0].getAttribute("data-rows")).toBe("3");
    expect(charts[1].getAttribute("data-rows")).toBe("6");
    // the non-standard `source` field must be stripped before handing the spec to Vega
    expect(charts[0].getAttribute("data-hassource")).toBe("false");
  });

  it("falls back to the builder (single chart) when there is no saved chart", async () => {
    render(<ChartMode cfg={cfg} />);
    await waitFor(() => expect(screen.getAllByTestId("vega")).toHaveLength(1));
  });
});
