// Reproduces the "load a saved report → grid shows the PREVIOUS view until you click again" bug
// (screenshot 2026-07-01: field list = ScenarioSet/Risk HHI, but grid still showed Sector rows with
// blank cells). Root cause was loadViewState firing a deferred reload() that closed over the stale
// cfg; the fix passes the new config explicitly to reload(next). This test drives the real component
// with AG Grid mocked to a plain table and fetch stubbed, and asserts the grid re-queries + re-renders
// with the loaded view's rows on the FIRST load.
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { vi, describe, it, expect, beforeEach } from "vitest";

// AG Grid -> a trivial table so jsdom can render rows/columns we can assert on.
vi.mock("ag-grid-react", () => ({
  AgGridReact: ({ rowData, columnDefs }: any) => (
    <table>
      <thead><tr>{columnDefs.map((c: any, i: number) => <th key={i}>{c.headerName}</th>)}</tr></thead>
      <tbody>
        {rowData.map((r: any, i: number) => <tr key={i}><td>{r.__label}</td></tr>)}
      </tbody>
    </table>
  ),
}));

import { Pivot } from "./Pivot";
import { AppProvider } from "../context/AppContext";
import { ContextBar } from "../shell/ContextBar";

const DIMS = {
  dimensions: ["Date", "Book", "Sector", "ScenarioSet"],
  measures: ["Net exposure", "Scenario VaR 99", "Risk HHI"],
  scenario_dependent: ["Scenario VaR 99", "Risk HHI"],
  members: { Date: ["2024-12-31"], ScenarioSet: ["HistFull"], Sector: [], Book: ["Soros"] },
  dates: ["2024-12-31"], scenario_sets: ["HistFull"],
};
const META = { dates: ["2024-12-31"], scenario_sets: ["HistFull", "Evt:COVID2020"], factors: [], ts_measures: [], by_levels: [] };

// the saved "Concentration — Risk HHI" view: ScenarioSet on rows, Risk HHI measure
const HHI_VIEW = {
  schema_version: 1, name: "Concentration — Risk HHI", path: "Public", created: "", updated: "",
  state: { rows: ["ScenarioSet"], cols: [], measures: ["Risk HHI"],
           filters: { Book: ["Soros"], Date: ["2024-12-31"] }, row_tot: false, render: "grid" },
};

function pivotRows(rowsParam: string) {
  // return distinct rows per requested dimension so we can tell which query populated the grid
  if (rowsParam.startsWith("Sector")) {
    return [{ Sector: "Financials", "Net exposure": 1 }, { Sector: "Energy", "Net exposure": 2 }];
  }
  if (rowsParam.startsWith("ScenarioSet")) {
    return [{ ScenarioSet: "HistFull", "Risk HHI": 0.03 }, { ScenarioSet: "Hypo:RiskOff", "Risk HHI": 0.18 }];
  }
  return [];
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const u = new URL(url, "http://x");
    const p = u.pathname;
    const json = (body: unknown) => ({ ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) });
    if (p.endsWith("/dims")) return json(DIMS);
    if (p.endsWith("/meta")) return json(META);
    if (p.endsWith("/views")) return json({ sections: { Public: { folders: {}, views: [
      { name: HHI_VIEW.name, slug: "concentration-hhi", path: "Public", file: "Public/concentration-hhi.json" }] },
      Private: { folders: {}, views: [] } } });
    if (p.includes("/views/item/")) return json(HHI_VIEW);
    if (p.endsWith("/pivot")) return json({
      rows: [], cols: [], measures: [], totals: false, warning: null,
      records: pivotRows(u.searchParams.get("rows") || ""),
    });
    return json({});
  }) as unknown as typeof fetch);
});

function renderPivot(withBar = false) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/pivot"]}>
        <AppProvider>
          {withBar && <ContextBar />}
          <Pivot />
        </AppProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function pivotCallsWithScenario(scen: string): boolean {
  const calls = (fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls;
  return calls.some(([url]) => typeof url === "string"
    && url.includes("/pivot") && decodeURIComponent(url).includes(`"ScenarioSet":["${scen}"]`));
}

describe("Pivot — loading a saved view", () => {
  it("shows the loaded view's rows on the FIRST click (not the previous view's stale rows)", async () => {
    renderPivot();

    // initial default view: Sector rows
    await waitFor(() => expect(screen.getByText("Financials")).toBeInTheDocument());

    // open the Repository and click the saved HHI view (one click)
    fireEvent.click(screen.getByText("Views"));
    await waitFor(() => expect(screen.getByText("Concentration — Risk HHI")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Concentration — Risk HHI"));

    // FIRST click must render the HHI view's ScenarioSet rows, and the stale Sector rows must be gone
    await waitFor(() => expect(screen.getByText("HistFull")).toBeInTheDocument());
    expect(screen.getByText("Hypo:RiskOff")).toBeInTheDocument();
    expect(screen.queryByText("Financials")).not.toBeInTheDocument();
  });

  it("updates the Fields section (R/C/F/M) to the loaded view", async () => {
    renderPivot();
    await waitFor(() => expect(screen.getByText("Financials")).toBeInTheDocument());

    // default filters chips include the context bar's ScenarioSet=HistFull
    expect(screen.getByText("ScenarioSet=HistFull")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Views"));
    await waitFor(() => expect(screen.getByText("Concentration — Risk HHI")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Concentration — Risk HHI"));

    // the loaded view's FILTERS replace the defaults: Book=Soros in, ScenarioSet=HistFull out
    await waitFor(() => expect(screen.getByText("Book=Soros")).toBeInTheDocument());
    expect(screen.getByText("Date=2024-12-31")).toBeInTheDocument();
    expect(screen.queryByText("ScenarioSet=HistFull")).not.toBeInTheDocument();
  });

  it("re-queries the cube when the scenario dropdown changes", async () => {
    renderPivot(true);
    await waitFor(() => expect(screen.getByText("Financials")).toBeInTheDocument());
    // default context scenario is folded into the pivot query
    await waitFor(() => expect(pivotCallsWithScenario("HistFull")).toBe(true));

    // change the context-bar scenario dropdown (the select currently holding "HistFull")
    const selects = screen.getAllByRole("combobox") as HTMLSelectElement[];
    const scen = selects.find((s) => s.value === "HistFull")!;
    fireEvent.change(scen, { target: { value: "Evt:COVID2020" } });

    // the pivot must re-query with the NEW scenario (numbers update, not just the chip)
    await waitFor(() => expect(pivotCallsWithScenario("Evt:COVID2020")).toBe(true));
  });
});
