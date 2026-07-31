// Multi-manager Phase 3/4: with >1 book loaded, /pivot rejects the three book-independent
// attribution measures (Factor contribution / Specific PnL / Realized PnL — baked columns with
// no Book key, risk_api.py's _validate_pivot) with HTTP 400 and a specific explanatory message.
// This proves the Vite Pivot lens surfaces that EXACT backend message (not a generic "request
// failed") — usePivot's reload() already threads ApiError.message straight into the `error` state
// Pivot.tsx renders, so this is a regression guard on that existing wiring, driven end-to-end
// through a saved view (same click flow the other Pivot tests use).
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { vi, describe, it, expect, beforeEach } from "vitest";

vi.mock("ag-grid-react", () => ({
  AgGridReact: ({ rowData, columnDefs }: any) => (
    <table>
      <thead><tr>{columnDefs.map((c: any, i: number) => <th key={i}>{c.headerName}</th>)}</tr></thead>
      <tbody>{rowData.map((r: any, i: number) => <tr key={i}><td>{r.__label}</td></tr>)}</tbody>
    </table>
  ),
}));

import { Pivot } from "./Pivot";
import { AppProvider } from "../context/AppContext";

const DIMS = {
  dimensions: ["Date", "Book", "Sector", "Factor", "ScenarioSet"],
  measures: ["Net exposure", "Scenario VaR 99", "Factor contribution"],
  scenario_dependent: ["Scenario VaR 99"],
  members: { Date: ["2024-12-31"], ScenarioSet: ["HistFull"], Sector: [], Factor: [], Book: ["Soros", "TigerGlobal"] },
  dates: ["2024-12-31"], scenario_sets: ["HistFull"],
};
const META = {
  dates: ["2024-12-31"], scenario_sets: ["HistFull"], factors: ["Market"], ts_measures: [], by_levels: [],
  managers: [
    { book: "Soros", entity_name: null, firm_type: null, cik: null, n_positions_distinct: null },
    { book: "TigerGlobal", entity_name: null, firm_type: null, cik: null, n_positions_distinct: null },
  ],
};

const REJECTION_MSG =
  "['Factor contribution'] are book-independent (baked columns with no Book key — a known atoti "
  + "0.9.15 limitation, see barra_factor_risk_cube.py) and cannot be trusted per-book with 2 books "
  + "loaded: they would silently read one arbitrary book's numbers under every book's label. Use "
  + "barra_pnl_attribution.py's book= precompute for correct per-book attribution instead.";

// the saved view a user picks that happens to select a book-independent measure
const REJECTED_VIEW = {
  schema_version: 1, name: "Factor contribution by name", path: "Public", created: "", updated: "",
  state: { rows: ["Factor"], cols: [], measures: ["Factor contribution"],
           filters: { Book: ["Soros"], Date: ["2024-12-31"] }, row_tot: false, render: "grid" },
};

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const u = new URL(url, "http://x");
    const p = u.pathname;
    const json = (body: unknown, status = 200) =>
      ({ ok: status < 400, status, json: async () => body, text: async () => JSON.stringify(body) });
    if (p.endsWith("/dims")) return json(DIMS);
    if (p.endsWith("/meta")) return json(META);
    if (p.endsWith("/views")) return json({ sections: { Public: { folders: {}, views: [
      { name: REJECTED_VIEW.name, slug: "factor-contrib", path: "Public", file: "Public/factor-contrib.json" }] },
      Private: { folders: {}, views: [] } } });
    if (p.includes("/views/item/")) return json(REJECTED_VIEW);
    if (p.endsWith("/pivot")) {
      const measures = (u.searchParams.get("measures") || "").split(",");
      if (measures.includes("Factor contribution")) {
        return json({ detail: REJECTION_MSG }, 400);
      }
      return json({
        rows: [], cols: [], measures: [], totals: false, warning: null,
        records: [{ Sector: "Financials", "Net exposure": 1 }],
      });
    }
    return json({});
  }) as unknown as typeof fetch);
});

function renderPivot() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/pivot"]}>
        <AppProvider><Pivot /></AppProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Pivot — book-independent measure rejection", () => {
  it("surfaces the backend's exact rejection message, not a generic failure", async () => {
    renderPivot();
    await waitFor(() => expect(screen.getByText("Financials")).toBeInTheDocument());

    fireEvent.click(screen.getByText("Views"));
    await waitFor(() => expect(screen.getByText("Factor contribution by name")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Factor contribution by name"));

    // the exact backend detail text appears verbatim — not "Request failed" or a blank grid
    await waitFor(() => expect(screen.getByText(REJECTION_MSG)).toBeInTheDocument());
    expect(screen.queryByText("Request failed")).toBeNull();
  });
});
