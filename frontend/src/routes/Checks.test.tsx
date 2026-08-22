// Limits calibration disclosure (multi-manager Phase 3/4, Task C): /limits gained additive
// `calibrated_for` / `cross_manager_thresholds` / `calibration_note` fields — a red breach must
// never read as if the thresholds were THIS manager's own when they were tuned for a different
// manager. The Checks lens must surface `calibration_note` when `cross_manager_thresholds` is
// true, and stay silent (no note at all) when it's false — an understated disclosure, not a
// shouted warning.
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { vi, describe, it, expect } from "vitest";

import { Checks } from "./Checks";
import { AppProvider } from "../context/AppContext";

const META = {
  dates: ["2026-06-30"], scenario_sets: ["HistFull"], factors: [], ts_measures: [], by_levels: [],
  managers: [
    { manager: "Soros", entity_name: null, firm_type: null, cik: null, n_positions_distinct: null },
    { manager: "TigerGlobal", entity_name: null, firm_type: null, cik: null, n_positions_distinct: null },
  ],
};
const DQ = { status: "pass", summary: { PASS: 1, WARN: 0, FAIL: 0 }, checks: [],
  latest_date: {}, stubs: { n_securities: 1, sector_unknown: 0, country_stub_US: 1 } };
const BT = { status: "insufficient" };

function limitsPayload(manager: string) {
  const crossManager = manager !== "Soros";
  return {
    date: "2026-06-30", set: "HistFull", manager, status: "green", configured: true, checks: [], breaches: [],
    calibrated_for: "Soros",
    cross_manager_thresholds: crossManager,
    calibration_note: crossManager
      ? `These thresholds were calibrated for the 'Soros' manager, not '${manager}' — the RAG `
        + "verdict above is being computed against another manager's limits and has not been "
        + "separately tuned for this manager's scale/strategy."
      : null,
  };
}

function mockFetch(manager: string) {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const u = new URL(url, "http://x");
    const json = (body: unknown) =>
      ({ ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) });
    if (u.pathname.endsWith("/meta")) return json(META);
    if (u.pathname.endsWith("/limits")) return json(limitsPayload(manager));
    if (u.pathname.endsWith("/dq")) return json(DQ);
    if (u.pathname.endsWith("/backtest")) return json(BT);
    return json({});
  }) as unknown as typeof fetch);
}

function renderChecks(manager: string) {
  mockFetch(manager);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/checks?manager=${manager}`]}>
        <AppProvider>
          <Checks />
        </AppProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Checks — limits calibration disclosure", () => {
  it("shows the calibration note when the limits were computed cross-manager", async () => {
    renderChecks("TigerGlobal");
    await waitFor(() => expect(screen.getByText(/calibrated for the 'Soros' manager/))
      .toBeInTheDocument());
    expect(screen.getByText(/not 'TigerGlobal'/)).toBeInTheDocument();
  });

  it("shows no calibration note at all for the manager the limits ARE calibrated for", async () => {
    renderChecks("Soros");
    await waitFor(() => expect(screen.getByText("Desk limits — HistFull")).toBeInTheDocument());
    expect(screen.queryByText(/calibrated for the/)).toBeNull();
  });
});
