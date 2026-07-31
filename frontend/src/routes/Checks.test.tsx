// Limits calibration disclosure (multi-manager Phase 3/4, Task C): /limits gained additive
// `calibrated_for` / `cross_book_thresholds` / `calibration_note` fields — a red breach must never
// read as if the thresholds were THIS book's own when they were tuned for a different book. The
// Checks lens must surface `calibration_note` when `cross_book_thresholds` is true, and stay
// silent (no note at all) when it's false — an understated disclosure, not a shouted warning.
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { vi, describe, it, expect } from "vitest";

import { Checks } from "./Checks";
import { AppProvider } from "../context/AppContext";

const META = {
  dates: ["2026-06-30"], scenario_sets: ["HistFull"], factors: [], ts_measures: [], by_levels: [],
  managers: [
    { book: "Soros", entity_name: null, firm_type: null, cik: null, n_positions_distinct: null },
    { book: "TigerGlobal", entity_name: null, firm_type: null, cik: null, n_positions_distinct: null },
  ],
};
const DQ = { status: "pass", summary: { PASS: 1, WARN: 0, FAIL: 0 }, checks: [],
  latest_date: {}, stubs: { n_securities: 1, sector_unknown: 0, country_stub_US: 1 } };
const BT = { status: "insufficient" };

function limitsPayload(book: string) {
  const crossBook = book !== "Soros";
  return {
    date: "2026-06-30", set: "HistFull", book, status: "green", configured: true, checks: [], breaches: [],
    calibrated_for: "Soros",
    cross_book_thresholds: crossBook,
    calibration_note: crossBook
      ? `These thresholds were calibrated for the 'Soros' book, not '${book}' — the RAG verdict `
        + "above is being computed against another book's limits and has not been separately "
        + "tuned for this book's scale/strategy."
      : null,
  };
}

function mockFetch(book: string) {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const u = new URL(url, "http://x");
    const json = (body: unknown) =>
      ({ ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) });
    if (u.pathname.endsWith("/meta")) return json(META);
    if (u.pathname.endsWith("/limits")) return json(limitsPayload(book));
    if (u.pathname.endsWith("/dq")) return json(DQ);
    if (u.pathname.endsWith("/backtest")) return json(BT);
    return json({});
  }) as unknown as typeof fetch);
}

function renderChecks(book: string) {
  mockFetch(book);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/checks?book=${book}`]}>
        <AppProvider>
          <Checks />
        </AppProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Checks — limits calibration disclosure", () => {
  it("shows the calibration note when the limits were computed cross-book", async () => {
    renderChecks("TigerGlobal");
    await waitFor(() => expect(screen.getByText(/calibrated for the 'Soros' book/))
      .toBeInTheDocument());
    expect(screen.getByText(/not 'TigerGlobal'/)).toBeInTheDocument();
  });

  it("shows no calibration note at all for the book the limits ARE calibrated for", async () => {
    renderChecks("Soros");
    await waitFor(() => expect(screen.getByText("Desk limits — HistFull")).toBeInTheDocument());
    expect(screen.queryByText(/calibrated for the/)).toBeNull();
  });
});
