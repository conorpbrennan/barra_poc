// Integration check for the guarded single-book lenses (multi-manager Phase 4): when /drift
// returns the book_mismatch status (risk_api.py's _book_guard — the requested book isn't the one
// the drift artifact covers), the Drift lens must render the quiet informational note, not an
// empty chart/table or a crash trying to read .series/.summary off a shape that doesn't have them.
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { vi, describe, it, expect, beforeEach } from "vitest";

import { Drift } from "./Drift";
import { AppProvider } from "../context/AppContext";

const META = {
  dates: ["2026-06-30"], scenario_sets: ["HistFull"], factors: [], ts_measures: [], by_levels: [],
  managers: [
    { book: "Soros", entity_name: null, firm_type: null, cik: null, n_positions_distinct: null },
    { book: "TigerGlobal", entity_name: null, firm_type: null, cik: null, n_positions_distinct: null },
  ],
};
const MISMATCH = {
  status: "book_mismatch", kind: "drift", requested_book: "TigerGlobal", artifact_book: "Soros",
  basis: "inferred from the live positions frame (exactly one Book present)",
  reason: "the drift artifact was computed for the 'Soros' book, not 'TigerGlobal' — serving it "
    + "under another book's label would be silently wrong data, not just stale data",
};

function mockFetch() {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const u = new URL(url, "http://x");
    const json = (body: unknown) =>
      ({ ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) });
    if (u.pathname.endsWith("/meta")) return json(META);
    if (u.pathname.endsWith("/drift")) return json(MISMATCH);
    return json({});
  }) as unknown as typeof fetch);
}

beforeEach(mockFetch);

function renderDrift() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/drift?book=TigerGlobal"]}>
        <AppProvider>
          <Drift />
        </AppProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Drift lens — book_mismatch", () => {
  it("renders the informational notice instead of an empty chart or a crash", async () => {
    renderDrift();
    await waitFor(() => expect(screen.getByText(/not available for/)).toBeInTheDocument());
    expect(screen.getByText("TigerGlobal", { selector: "strong" })).toBeInTheDocument();
    expect(screen.getByText("Soros", { selector: "strong" })).toBeInTheDocument();
    // never falls through to a table/chart built from fields the mismatch shape doesn't have
    expect(screen.queryByText(/Drift attribution/)).toBeNull();
    expect(document.querySelector("table")).toBeNull();
  });
});
