// Entity selector (multi-manager Phase 4): the manager control must be plain text with one
// manager (a one-option dropdown is chartjunk — Tufte/Few) and a real <select> once a second
// manager exists, sourced entirely from /meta.managers (never a hardcoded list), with the
// known-bad live "N/A" Manager member filtered out defensively (see
// scratchpad/phase3-notes.md item 5).
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
function ManagerProbe() {
  const { manager } = useApp();
  return <div data-testid="manager-probe">{manager}</div>;
}

function renderBar(managers: unknown[]) {
  mockFetch(managers);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const utils = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/"]}>
        <AppProvider>
          <ContextBar />
          <ManagerProbe />
        </AppProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  // scope to the context bar itself so assertions never collide with the ManagerProbe's own text
  const bar = () => within(utils.container.querySelector(".contextbar") as HTMLElement);
  return { ...utils, bar };
}

const SOROS_ONLY = [
  { manager: "Soros", entity_name: null, firm_type: null, cik: null, n_positions_distinct: null },
];
const TWO_MANAGERS = [
  { manager: "Soros", entity_name: "SOROS FUND MANAGEMENT LLC", firm_type: "hedge_fund", cik: 1029160, n_positions_distinct: 184 },
  { manager: "TigerGlobal", entity_name: "TIGER GLOBAL MANAGEMENT LLC", firm_type: "hedge_fund", cik: 1167483, n_positions_distinct: 197 },
];

describe("ContextBar — manager control", () => {
  it("renders as plain text (no dropdown) when /meta has a single manager", async () => {
    const { bar } = renderBar(SOROS_ONLY);
    await waitFor(() => expect(bar().getByText("Soros")).toBeInTheDocument());
    // exactly two selects (As-of, Scenario) — no third select for Manager
    expect(bar().getAllByRole("combobox").length).toBe(2);
  });

  it("renders a real <select> once a second manager exists, labelled with entity attributes", async () => {
    const { bar } = renderBar(TWO_MANAGERS);
    await waitFor(() => expect(bar().getAllByRole("combobox").length).toBe(3));
    const combos = bar().getAllByRole("combobox") as HTMLSelectElement[];
    const managerSelect = combos.find((s) =>
      [...s.options].some((o) => o.textContent?.includes("hedge_fund")))!;
    expect(managerSelect).toBeTruthy();
    const labels = [...managerSelect.options].map((o) => o.textContent);
    expect(labels).toEqual([
      "SOROS FUND MANAGEMENT LLC · hedge_fund",
      "TIGER GLOBAL MANAGEMENT LLC · hedge_fund",
    ]);
  });

  it("selecting a manager threads the new manager through app context", async () => {
    const { bar } = renderBar(TWO_MANAGERS);
    await waitFor(() => expect(bar().getAllByRole("combobox").length).toBe(3));
    expect(screen.getByTestId("manager-probe").textContent).toBe("Soros");

    const combos = bar().getAllByRole("combobox") as HTMLSelectElement[];
    const managerSelect = combos.find((s) => s.value === "Soros")!;
    fireEvent.change(managerSelect, { target: { value: "TigerGlobal" } });

    await waitFor(() => expect(screen.getByTestId("manager-probe").textContent).toBe("TigerGlobal"));
  });

  it("filters out the known-bad 'N/A' manager member defensively", async () => {
    const { bar } = renderBar([
      { manager: "N/A", entity_name: null, firm_type: null, cik: null, n_positions_distinct: null },
      ...SOROS_ONLY]);
    await waitFor(() => expect(bar().getByText("Soros")).toBeInTheDocument());
    // still single real manager after filtering -> plain text, not a Manager selector
    expect(bar().getAllByRole("combobox").length).toBe(2);
    expect(bar().queryByText("N/A")).toBeNull();
  });
});
