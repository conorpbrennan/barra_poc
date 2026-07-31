// Entity selector (multi-manager Phase 4): the book control must be plain text with one manager
// (a one-option dropdown is chartjunk — Tufte/Few) and a real <select> once a second manager
// exists, sourced entirely from /meta.managers (never a hardcoded list), with the known-bad
// live "N/A" Book member filtered out defensively (see scratchpad/phase3-notes.md item 5).
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { vi, describe, it, expect } from "vitest";

import { ContextBar } from "./ContextBar";
import { AppProvider, useApp } from "../context/AppContext";

const BASE_META = {
  dates: ["2026-06-30"], scenario_sets: ["HistFull"], factors: [], ts_measures: [], by_levels: [],
};

function mockFetch(managers: unknown[]) {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const u = new URL(url, "http://x");
    const json = (body: unknown) =>
      ({ ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) });
    if (u.pathname.endsWith("/meta")) return json({ ...BASE_META, managers });
    return json({});
  }) as unknown as typeof fetch);
}

// a tiny consumer so tests can assert the CONTEXT value (not just the rendered label) changes
function BookProbe() {
  const { book } = useApp();
  return <div data-testid="book-probe">{book}</div>;
}

function renderBar(managers: unknown[]) {
  mockFetch(managers);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const utils = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/"]}>
        <AppProvider>
          <ContextBar />
          <BookProbe />
        </AppProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  // scope to the context bar itself so assertions never collide with the BookProbe's own text
  const bar = () => within(utils.container.querySelector(".contextbar") as HTMLElement);
  return { ...utils, bar };
}

const SOROS_ONLY = [
  { book: "Soros", entity_name: null, firm_type: null, cik: null, n_positions_distinct: null },
];
const TWO_MANAGERS = [
  { book: "Soros", entity_name: "SOROS FUND MANAGEMENT LLC", firm_type: "hedge_fund", cik: 1029160, n_positions_distinct: 184 },
  { book: "TigerGlobal", entity_name: "TIGER GLOBAL MANAGEMENT LLC", firm_type: "hedge_fund", cik: 1167483, n_positions_distinct: 197 },
];

describe("ContextBar — book control", () => {
  it("renders as plain text (no dropdown) when /meta has a single manager", async () => {
    const { bar } = renderBar(SOROS_ONLY);
    await waitFor(() => expect(bar().getByText("Soros")).toBeInTheDocument());
    // exactly two selects (As-of, Scenario) — no third select for Book
    expect(bar().getAllByRole("combobox").length).toBe(2);
  });

  it("renders a real <select> once a second manager exists, labelled with entity attributes", async () => {
    const { bar } = renderBar(TWO_MANAGERS);
    await waitFor(() => expect(bar().getAllByRole("combobox").length).toBe(3));
    const combos = bar().getAllByRole("combobox") as HTMLSelectElement[];
    const bookSelect = combos.find((s) =>
      [...s.options].some((o) => o.textContent?.includes("hedge_fund")))!;
    expect(bookSelect).toBeTruthy();
    const labels = [...bookSelect.options].map((o) => o.textContent);
    expect(labels).toEqual([
      "SOROS FUND MANAGEMENT LLC · hedge_fund",
      "TIGER GLOBAL MANAGEMENT LLC · hedge_fund",
    ]);
  });

  it("selecting a manager threads the new book through app context", async () => {
    const { bar } = renderBar(TWO_MANAGERS);
    await waitFor(() => expect(bar().getAllByRole("combobox").length).toBe(3));
    expect(screen.getByTestId("book-probe").textContent).toBe("Soros");

    const combos = bar().getAllByRole("combobox") as HTMLSelectElement[];
    const bookSelect = combos.find((s) => s.value === "Soros")!;
    fireEvent.change(bookSelect, { target: { value: "TigerGlobal" } });

    await waitFor(() => expect(screen.getByTestId("book-probe").textContent).toBe("TigerGlobal"));
  });

  it("filters out the known-bad 'N/A' book member defensively", async () => {
    const { bar } = renderBar([
      { book: "N/A", entity_name: null, firm_type: null, cik: null, n_positions_distinct: null },
      ...SOROS_ONLY]);
    await waitFor(() => expect(bar().getByText("Soros")).toBeInTheDocument());
    // still single real manager after filtering -> plain text, not a Book selector
    expect(bar().getAllByRole("combobox").length).toBe(2);
    expect(bar().queryByText("N/A")).toBeNull();
  });
});
